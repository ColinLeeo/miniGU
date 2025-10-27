use gql_parser::ast::{
    BinaryOp, BooleanLiteral, Expr, Ident, Literal, NonNegativeInteger, StringLiteral,
    StringLiteralKind, UnsignedInteger, UnsignedIntegerKind, UnsignedNumericLiteral, Value,
};
use minigu_common::constants::SESSION_USER;
use minigu_common::data_type::LogicalType;
use minigu_common::error::not_implemented;
use minigu_common::value::ScalarValue;

use super::error::{BindError, BindResult};
use super::Binder;
use crate::bound::{BoundBinaryOp, BoundExpr, BoundExprKind, BoundUnsignedInteger};

impl Binder<'_> {
    pub fn bind_value_expression(&self, expr: &Expr) -> BindResult<BoundExpr> {
        match expr {
            Expr::Binary { op, left, right } => {
                let l = self.bind_value_expression(&left.value())?;
                let r = self.bind_value_expression(&right.value())?;
                self.bind_binary(op.value(), &l, &r)
            }
            Expr::Unary { .. } => not_implemented("unary expression", None),
            Expr::DurationBetween { .. } => not_implemented("duration between expression", None),
            Expr::Is { .. } => not_implemented("is expression", None),
            Expr::IsNot { .. } => not_implemented("is not expression", None),
            Expr::Function(_) => not_implemented("function expression", None),
            Expr::Aggregate(_) => not_implemented("aggregate expression", None),
            Expr::Variable(variable) => {
                let field = self
                    .active_data_schema
                    .as_ref()
                    .ok_or_else(|| BindError::VariableNotFound(variable.clone()))?
                    .get_field_by_name(variable)
                    .ok_or_else(|| BindError::VariableNotFound(variable.clone()))?;
                Ok(BoundExpr::variable(
                    variable.to_string(),
                    field.ty().clone(),
                    field.is_nullable(),
                ))
            }
            Expr::Value(value) => bind_value(value),
            Expr::Path(_) => not_implemented("path expression", None),
            Expr::Property {
                source,
                trailing_names,
            } => self.bind_property(
                source.value(),
                trailing_names.iter().map(|n| n.value()).cloned().collect(),
            ),
            Expr::Graph(_) => not_implemented("graph expression", None),
        }
    }

    pub fn bind_property(
        &self,
        source: &Expr,
        trailing_names: Vec<Ident>,
    ) -> BindResult<BoundExpr> {
        if trailing_names.is_empty() {
            return Err(BindError::EmptyPropertyPath);
        }

        if trailing_names.len() != 1 {
            return not_implemented("Property chaining not supported", None);
        }

        let base_var = match source {
            Expr::Variable(var) => var,
            _ => return Err(BindError::Unexpected),
        };

        let schema = self
            .active_data_schema
            .as_ref()
            .ok_or_else(|| BindError::VariableNotFound(base_var.clone()))?;

        let rec_field = schema
            .get_field_by_name(base_var.as_str())
            .ok_or_else(|| BindError::VariableNotFound(base_var.clone()))?;
        let mut current_ty = rec_field.ty().clone();
        let mut curent_nullable = rec_field.is_nullable();

        // TODO: Support idx
        let mut last_idx = 0usize;
        let field_name = trailing_names[0].as_str();
        Ok(BoundExpr {
            kind: BoundExprKind::Property {
                base: base_var.to_string(),
                field: field_name.to_string(),
                field_idx: last_idx,
            },
            logical_type: current_ty,
            nullable: curent_nullable,
        })
    }

    pub fn bind_binary(
        &self,
        op: &BinaryOp,
        left: &BoundExpr,
        right: &BoundExpr,
    ) -> BindResult<BoundExpr> {
        match op {
            BinaryOp::Eq
            | BinaryOp::Ne
            | BinaryOp::Lt
            | BinaryOp::Le
            | BinaryOp::Gt
            | BinaryOp::Ge => Ok(BoundExpr {
                kind: BoundExprKind::Binary {
                    op: bind_binary_op(&op),
                    left: Box::new(left.clone()),
                    right: Box::new(right.clone()),
                },
                logical_type: LogicalType::Boolean,
                nullable: left.nullable && right.nullable,
            }),
            _ => not_implemented("only support eq,ne,lt,le,gt,ge operation", None),
        }
    }

    pub fn bind_non_negative_integer(
        &self,
        integer: &NonNegativeInteger,
    ) -> BindResult<BoundUnsignedInteger> {
        match integer {
            NonNegativeInteger::Integer(unsigned) => bind_unsigned_integer(unsigned),
            NonNegativeInteger::Parameter(_) => {
                not_implemented("parameterized non-negative integer", None)
            }
        }
    }
}

pub fn bind_binary_op(op: &BinaryOp) -> BoundBinaryOp {
    match op {
        BinaryOp::Add => BoundBinaryOp::Add,
        BinaryOp::Sub => BoundBinaryOp::Sub,
        BinaryOp::Mul => BoundBinaryOp::Mul,
        BinaryOp::Div => BoundBinaryOp::Div,
        BinaryOp::Concat => BoundBinaryOp::Concat,
        BinaryOp::Or => BoundBinaryOp::Or,
        BinaryOp::Xor => BoundBinaryOp::Xor,
        BinaryOp::And => BoundBinaryOp::And,
        BinaryOp::Lt => BoundBinaryOp::Lt,
        BinaryOp::Le => BoundBinaryOp::Le,
        BinaryOp::Gt => BoundBinaryOp::Gt,
        BinaryOp::Ge => BoundBinaryOp::Ge,
        BinaryOp::Eq => BoundBinaryOp::Eq,
        BinaryOp::Ne => BoundBinaryOp::Ne,
    }
}

pub fn bind_value(value: &Value) -> BindResult<BoundExpr> {
    match value {
        Value::SessionUser => Ok(BoundExpr::value(
            SESSION_USER.into(),
            LogicalType::String,
            false,
        )),
        Value::Parameter(_) => not_implemented("parameter value", None),
        Value::Literal(literal) => bind_literal(literal),
    }
}

pub fn bind_literal(literal: &Literal) -> BindResult<BoundExpr> {
    match literal {
        Literal::Numeric(literal) => bind_numeric_literal(literal),
        Literal::Boolean(literal) => Ok(bind_boolean_literal(literal)),
        Literal::String(literal) => bind_string_literal(literal),
        Literal::Temporal(_) => not_implemented("temporal literal", None),
        Literal::Duration(_) => not_implemented("duration literal", None),
        Literal::List(_) => not_implemented("list literal", None),
        Literal::Record(_) => not_implemented("record literal", None),
        Literal::Null => Ok(BoundExpr::value(ScalarValue::Null, LogicalType::Null, true)),
    }
}

pub fn bind_numeric_literal(literal: &UnsignedNumericLiteral) -> BindResult<BoundExpr> {
    match literal {
        UnsignedNumericLiteral::Integer(integer) => {
            let unsigned = bind_unsigned_integer(integer.value())?;
            let expr = match unsigned {
                BoundUnsignedInteger::Int8(value) => {
                    BoundExpr::value(value.into(), LogicalType::Int8, false)
                }
                BoundUnsignedInteger::Int16(value) => {
                    BoundExpr::value(value.into(), LogicalType::Int16, false)
                }
                BoundUnsignedInteger::Int32(value) => {
                    BoundExpr::value(value.into(), LogicalType::Int32, false)
                }
                BoundUnsignedInteger::Int64(value) => {
                    BoundExpr::value(value.into(), LogicalType::Int64, false)
                }
            };
            Ok(expr)
        }
    }
}

pub fn bind_unsigned_integer(integer: &UnsignedInteger) -> BindResult<BoundUnsignedInteger> {
    match integer.kind {
        UnsignedIntegerKind::Binary => not_implemented("binary integer literal", None),
        UnsignedIntegerKind::Octal => not_implemented("octal integer literal", None),
        UnsignedIntegerKind::Decimal => {
            if let Ok(value) = integer.integer.parse::<i8>() {
                Ok(BoundUnsignedInteger::Int8(value))
            } else if let Ok(value) = integer.integer.parse::<i16>() {
                Ok(BoundUnsignedInteger::Int16(value))
            } else if let Ok(value) = integer.integer.parse::<i32>() {
                Ok(BoundUnsignedInteger::Int32(value))
            } else if let Ok(value) = integer.integer.parse::<i64>() {
                Ok(BoundUnsignedInteger::Int64(value))
            } else {
                Err(BindError::InvalidInteger(integer.integer.clone()))
            }
        }
        UnsignedIntegerKind::Hex => not_implemented("hex integer literal", None),
    }
}

pub fn bind_boolean_literal(literal: &BooleanLiteral) -> BoundExpr {
    match literal {
        BooleanLiteral::True => BoundExpr::value(true.into(), LogicalType::Boolean, false),
        BooleanLiteral::False => BoundExpr::value(false.into(), LogicalType::Boolean, false),
        // TODO: Is it OK to treat `unknown` as `null` here?
        BooleanLiteral::Unknown => {
            BoundExpr::value(ScalarValue::Boolean(None), LogicalType::Boolean, true)
        }
    }
}

pub fn bind_string_literal(literal: &StringLiteral) -> BindResult<BoundExpr> {
    match literal.kind {
        StringLiteralKind::Char => Ok(BoundExpr::value(
            literal.literal.as_str().into(),
            LogicalType::String,
            false,
        )),
        StringLiteralKind::Byte => not_implemented("byte string literal", None),
    }
}
