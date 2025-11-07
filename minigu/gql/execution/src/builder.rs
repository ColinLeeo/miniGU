use std::any::Any;
use std::sync::Arc;

use arrow::array::{AsArray, Int32Array};
use minigu_catalog::property::Property;
use minigu_catalog::provider::GraphProvider;
use minigu_common::data_chunk::DataChunk;
use minigu_common::data_type::{DataSchema, LogicalType};
use minigu_common::types::{PropertyId, VertexIdArray};
use minigu_context::graph::GraphContainer;
use minigu_context::session::SessionContext;
use minigu_planner::bound::{BoundExpr, BoundExprKind};
use minigu_planner::plan::{PlanData, PlanNode};

use crate::evaluator::binary::{Binary, BinaryOp};
use crate::evaluator::column_ref::ColumnRef;
use crate::evaluator::constant::Constant;
use crate::evaluator::BoxedEvaluator;
use crate::executor::procedure_call::ProcedureCallBuilder;
use crate::executor::sort::SortSpec;
use crate::executor::{BoxedExecutor, Executor, IntoExecutor};
use crate::source::{VertexPropertySource, VertexSource};
use minigu_planner::bound::BoundBinaryOp;

const DEFAULT_CHUNK_SIZE: usize = 2048;

/// extract property id from bound expr.
fn extract_property_ids(expr: &BoundExpr) -> Vec<PropertyId> {
    let mut property_ids = std::collections::HashSet::new();
    
    fn collect_property_ids_recursive(expr: &BoundExpr, property_ids: &mut std::collections::HashSet<PropertyId>) {
        match &expr.kind {
            BoundExprKind::Property { field_idx, .. } => {
                property_ids.insert(PropertyId::from(*field_idx as u32));
            }
            BoundExprKind::Binary { left, right, .. } => {
                collect_property_ids_recursive(left, property_ids);
                collect_property_ids_recursive(right, property_ids);
            }
            BoundExprKind::Value(_) | BoundExprKind::Variable(_) => {
                // SKIP
            }
        }
    }
    
    collect_property_ids_recursive(expr, &mut property_ids);
    let mut result: Vec<PropertyId> = property_ids.into_iter().collect();
    result.sort();
    result
}

pub struct ExecutorBuilder {
    session: SessionContext,
}

impl ExecutorBuilder {
    pub fn new(session: SessionContext) -> Self {
        Self { session }
    }

    pub fn build(self, physical_plan: &PlanNode) -> BoxedExecutor {
        self.build_executor(physical_plan)
    }

    fn build_executor(&self, physical_plan: &PlanNode) -> BoxedExecutor {
        let children = physical_plan.children();
        match physical_plan {
            PlanNode::PhysicalFilter(filter) => {
                assert_eq!(children.len(), 1);
                let child_plan = &children[0];

                let executor = if matches!(child_plan, PlanNode::PhysicalNodeScan(_)) {
                    let property_ids = extract_property_ids(&filter.predicate);
                    
                    let provider: Arc<dyn GraphProvider> = Arc::clone(self.session.current_graph.as_ref().unwrap().object());

                    let container: Arc<GraphContainer> =provider
                        .downcast_arc::<GraphContainer>()
                        .expect("current graph must be GraphContainer");
                    
                    let src: Arc<dyn VertexPropertySource + Send + Sync> = container;

                    let node_scan_executor = self.build_executor(child_plan);

                    let nodeid_column_index = 0;
                    
                    let executor_with_props: BoxedExecutor = if property_ids.is_empty() {
                        Box::new(node_scan_executor.scan_vertex_property(nodeid_column_index, src, vec![]))
                    } else {
                        Box::new(node_scan_executor.scan_vertex_property(nodeid_column_index, src, property_ids.clone()))
                    };

                    let property_ids_for_evaluator = property_ids;
                    let predicate = self.build_evaluator_with_property_mapping(
                        &filter.predicate,
                        child_plan.schema().expect("child should have a schema"),
                        nodeid_column_index,
                        &property_ids_for_evaluator,
                    );
                    
                    Box::new(executor_with_props.filter(move |c| {
                        predicate
                            .evaluate(c)
                            .map(|a| a.into_array().as_boolean().clone())
                    })) as BoxedExecutor
                } else {
                    let schema = child_plan.schema().expect("child should have a schema");
                    let predicate = self.build_evaluator(&filter.predicate, schema);
                    Box::new(self.build_executor(child_plan).filter(move |c| {
                        predicate
                            .evaluate(c)
                            .map(|a| a.into_array().as_boolean().clone())
                    })) as BoxedExecutor
                };
                
                executor
            }
            PlanNode::PhysicalNodeScan(_node_scan) => {
                // Need to handle node scan for other path and query.
                assert_eq!(children.len(), 0);
                let cur_schema = self
                    .session
                    .home_schema
                    .as_ref()
                    .expect("there should be a home schema");
                // current set match on current graph.
                let cur_graph = self
                    .session
                    .current_graph
                    .as_ref()
                    .expect("there should be a current graph")
                    .object()
                    .as_ref();
                let provider: &dyn GraphProvider = cur_graph;
                let container = provider
                    .downcast_ref::<GraphContainer>()
                    .expect("current graph must be GraphContainer");
                let batches = container.vertex_source(&[], 1024).expect("err there");
                let source = batches.map(|arr: Arc<VertexIdArray>| Ok(arr));
                Box::new(source.scan_vertex())
            }
            PlanNode::PhysicalProject(project) => {
                assert_eq!(children.len(), 1);
                let schema = children[0].schema().expect("child should have a schema");
                let evaluators = project
                    .exprs
                    .iter()
                    .map(|e| self.build_evaluator(e, schema))
                    .collect();
                Box::new(self.build_executor(&children[0]).project(evaluators))
            }
            PlanNode::PhysicalCall(call) => {
                assert!(children.is_empty());
                let procedure = call.procedure.object().clone();
                let session = self.session.clone();
                let args = call.args.clone();
                Box::new(ProcedureCallBuilder::new(procedure, session, args).into_executor())
            }
            // We don't need an independent executor for PhysicalOneRow. Returning a chunk with a
            // single row is enough.
            PlanNode::PhysicalOneRow(one_row) => {
                assert!(children.is_empty());
                let schema = &one_row.schema().expect("one_row should have a data schema");
                assert_eq!(schema.fields().len(), 1);
                let field = &schema.fields()[0];
                assert_eq!(field.ty(), &LogicalType::Int32);
                assert!(!field.is_nullable());
                let columns = vec![Arc::new(Int32Array::from_iter_values([0])) as _];
                let chunk = DataChunk::new(columns);
                Box::new([Ok(chunk)].into_executor())
            }
            PlanNode::PhysicalSort(sort) => {
                assert_eq!(children.len(), 1);
                let schema = children[0].schema().expect("child should have a schema");
                let specs = sort
                    .specs
                    .iter()
                    .map(|s| {
                        let key = self.build_evaluator(&s.key, schema);
                        SortSpec::new(key, s.ordering, s.null_ordering)
                    })
                    .collect();
                Box::new(
                    self.build_executor(&children[0])
                        .sort(specs, DEFAULT_CHUNK_SIZE),
                )
            }
            PlanNode::PhysicalLimit(limit) => {
                assert_eq!(children.len(), 1);
                Box::new(self.build_executor(&children[0]).limit(limit.limit))
            }
            _ => unreachable!(),
        }
    }

    fn build_evaluator(&self, expr: &BoundExpr, schema: &DataSchema) -> BoxedEvaluator {
        self.build_evaluator_with_property_mapping(expr, schema, 0, &[])
    }

    fn build_evaluator_with_property_mapping(
        &self,
        expr: &BoundExpr,
        schema: &DataSchema,
        base_column_index: usize,
        property_ids: &[PropertyId],
    ) -> BoxedEvaluator {
        match &expr.kind {
            BoundExprKind::Value(value) => Box::new(Constant::new(value.clone())),
            BoundExprKind::Variable(variable) => {
                let index = schema
                    .get_field_index_by_name(variable)
                    .expect("variable should be present in the schema");
                Box::new(ColumnRef::new(index))
            }
            BoundExprKind::Property { field_idx, .. } => {
                let property_id = PropertyId::from(*field_idx as u32);
                let property_index_in_list = property_ids
                    .iter()
                    .position(|&pid| pid == property_id)
                    .unwrap_or_else(|| {
                        if property_ids.is_empty() {
                            *field_idx
                        } else {
                            panic!("property {} should be in property_ids list", property_id)
                        }
                    });
                todo!()
                // Box::new(Property::new(base_column_index, property_index_in_list))
            }
            BoundExprKind::Binary { op, left, right } => {
                let l = self.build_evaluator_with_property_mapping(left, schema, base_column_index, property_ids);
                let r = self.build_evaluator_with_property_mapping(right, schema, base_column_index, property_ids);
                let binary_op = match op {
                    BoundBinaryOp::Add => BinaryOp::Add,
                    BoundBinaryOp::Sub => BinaryOp::Sub,
                    BoundBinaryOp::Mul => BinaryOp::Mul,
                    BoundBinaryOp::Div => BinaryOp::Div,
                    BoundBinaryOp::And => BinaryOp::And,
                    BoundBinaryOp::Or => BinaryOp::Or,
                    BoundBinaryOp::Eq => BinaryOp::Eq,
                    BoundBinaryOp::Ne => BinaryOp::Ne,
                    BoundBinaryOp::Lt => BinaryOp::Lt,
                    BoundBinaryOp::Le => BinaryOp::Le,
                    BoundBinaryOp::Gt => BinaryOp::Gt,
                    BoundBinaryOp::Ge => BinaryOp::Ge,
                    BoundBinaryOp::Concat | BoundBinaryOp::Xor => {
                        panic!("unsupported binary operation: {:?}", op)
                    }
                };
                Box::new(Binary::new(binary_op, l, r))
            }
        }
    }
}
