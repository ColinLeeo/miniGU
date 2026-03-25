# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language

Always respond in Chinese (中文).

## Build & Development Commands

**Toolchain:** Nightly Rust (`nightly-2025-12-08`), specified in `rust-toolchain.toml`. Always pass `--features std,serde,miette` when building or testing.

```bash
# Build
cargo build --features std,serde,miette
cargo build -r --features std,serde,miette   # release

# Run the interactive shell
cargo run -- shell
cargo run -- execute <script.gql>

# Tests (CI uses nextest)
cargo nextest run --features std,serde,miette
cargo test --features std,serde,miette --doc  # doc tests only
cargo test -p <crate> --test <test_name>      # single test

# Lint & format
cargo clippy --tests --features std,serde,miette --no-deps
cargo fmt
cargo fmt --check
taplo fmt --check --diff   # TOML files
```

## Codebase Architecture

### Workspace layout

The repo is a Cargo workspace. Core logic lives in `minigu/` sub-crates; the CLI lives in `minigu-cli/`; end-to-end tests live in `minigu-test/`.

```
minigu/
  core/         # Public API surface: Database, Session
  context/      # SessionContext, DatabaseContext, GraphContainer
  catalog/      # In-memory schema catalog (hierarchy: directory → schema → graph/procedure)
  transaction/  # MVCC primitives: Timestamp, UndoEntry<T>, GraphTxnManager
  storage/      # TP (OLTP) and AP (OLAP) graph storage engines, WAL, DiskANN vector index
  gql/
    parser/     # GQL lexer + parser → AST (supports no_std)
    planner/    # Binder → LogicalPlanner → Optimizer → PhysicalPlan
    execution/  # Volcano-model executors, expression evaluators
minigu-cli/     # Interactive shell (rustyline) + script executor
minigu-test/    # sqllogictest + insta snapshot tests
```

### Query pipeline

A query enters through `Session::query()` (`minigu/core/src/session.rs`) and flows:

1. **Parse** — `parse_gql()` → `gql_parser::ast::Procedure`
2. **Plan** — Planner runs three sub-passes:
   - *Binder*: resolves names against the catalog
   - *LogicalPlanner*: builds a logical plan tree
   - *Optimizer*: converts to a physical plan tree
3. **Execute** — `ExecutorBuilder::build()` constructs a pull-based executor tree; calling `.next_chunk()` in a loop yields `DataChunk` batches (default batch size 2048, Arrow columnar layout)
4. **Format** — The CLI formats chunks as a table (sharp/csv/etc.) and optionally prints query metrics

### Storage & MVCC

**TP mode** (`minigu/storage/src/tp/`) is the default transactional engine:
- Vertices and edges each have a `VersionChain` holding the current version plus a linked undo log of `UndoEntry<DeltaOp>`.
- Visibility: if `commit_ts == txn_id` the current transaction wrote it; if `commit_ts ≤ start_ts` it was committed before the transaction began; otherwise walk the undo chain.
- WAL entries (`RedoEntry` with LSN, txn_id, operation) are serialized with postcard into a single `.minigu` file that also contains a checkpoint region.
- `InMemoryPersistence` (no disk) and `DbFilePersistence` (single-file with crash recovery) are swappable providers.

**AP mode** (`minigu/storage/src/ap/`) is column-oriented for analytical queries.

### Execution model

Executors live in `minigu/gql/execution/src/executor/` and implement the `Executor` trait (pull-based, `next_chunk() → Option<DataChunk>`). Expression evaluation is done by *evaluators* (`minigu/gql/execution/src/evaluator/`) that operate over Arrow arrays. The `ExecutorBuilder` (`builder.rs`) recursively converts a physical plan tree into an executor tree.

### Workflow

- After modifying Rust code, always run `cargo fmt` to format the code before finishing.
- All script/benchmark test results must be output to `experiment/result/`, organized by corresponding subdirectory paths (e.g., `experiment/result/comparison/`, `experiment/result/qerror/`).

### Key patterns

- **Feature flags:** The parser targets `no_std`; features `std`, `serde`, and `miette` must be explicitly enabled for normal use.
- **Error handling:** `thiserror` + `miette` for diagnostics; each crate has its own `Error` enum and a local `Result<T>` alias.
- **Concurrency:** `Arc`/`RwLock` for shared state; `DashMap`/`DashSet` for concurrent maps; `rayon` for parallelism.
- **Catalog hierarchy:** `MemoryCatalog` → `MemoryDirectoryCatalog` → `MemorySchemaCatalog` → graphs/procedures.

### GCard: Graph Cardinality Estimation（核心算法）

GCard 是基于度序列分段常数函数（Piecewise Constant Function, PCF）的**图模式匹配基数估计**算法。代码位于 `minigu/core/src/procedures/gcard_query/`。

#### 模块结构

```
procedures/gcard_query/
  mod.rs              # 入口：注册 gcard_query procedure，参数解析，结果选择
  types.rs            # Query/Predicate/AbstractEdge 等核心类型，JSON 反序列化
  query_graph.rs      # QueryGraph：查询图构建、环检测、生成树枚举、路径分解
  abs_graph.rs        # AbstractGraph：抽象图构建、拓扑排序、get_es() 基数计算
  degreepiecewise/    # PCF（分段常数函数）实现：alpha/beta 运算
  catalog.rs          # DegreeSeqGraphCompressed：预计算的度序列统计信息
  create_catalog.rs   # GCard_build procedure：遍历图数据构建统计 catalog
  statistic.rs        # Statistic：label → path pattern → 直方图
  block_statistic.rs  # BlockStatistic：压缩直方图
  graph.rs            # 基础图骨架
  union_find.rs       # 并查集，用于环检测
  update_log.rs       # 增量更新日志
  compact_update_log.rs # 日志压缩
  compression.rs      # 数据压缩
  stat_quality.rs     # 统计质量检查
```

#### 算法流程

```
输入: Query JSON (vertices, edges, predicates)
  ↓
[1] 解析 → QueryGraph (邻接表 + 谓词索引)
  ↓
[2] 环检测 (union-find)
  ├─ 无环: 直接构建单个 AbstractGraph
  └─ 有环: 枚举 k-best 生成树（基于边评分的贪心策略）
  ↓
[3] 对每棵生成树:
  a) 找 pivot 节点 (度 ≥ 3)
  b) 路径分解：pivot 之间的简单路径
  c) 压缩为 AbstractGraph（路径 → 抽象边）
  d) 从 DegreeSeqGraphCompressed 填充每条边的 PCF
  ↓
[4] 基数计算 (get_es):
  - 拓扑排序，从叶到根自底向上归约
  - PCF 运算: alpha（逐点乘积）/ beta_left / beta_right
  ↓
[5] 取所有 AbstractGraph 中最小非零基数作为最终估计
  - OUTER 模式: 基数 × 谓词选择率
  ↓
输出: cardinality (Int64)
```

#### 谓词处理模式

- **INNER (0)**: 在抽象图构建阶段就应用谓词（融入 PCF 计算）
- **OUTER (1)**: 先算无谓词基数，再乘以选择率
- **IGNORE (2)**: 完全忽略谓词

#### 使用方式

```sql
-- 前置：构建统计 catalog
CALL gcard_build(...)

-- 查询基数估计
CALL gcard_query("<query.json>", <max_path_length>, <sample_size>, <pred_type>, <verbose>, <max_subgraphs>)
```

#### 实验

查询模式定义在 `experiment/pattern/LDBC/`，包含 6 种基础模式（L1-L6: chain/cycle/triangle/star/path/cycle+branch）× 2 种谓词风格（PA 聚集 / PB 分散）= 12 种配置。基准对比工具包括 pathce、gcare-wj、color 等。

minigu> call bench_catalog("ldbc", 2, 1);
===== bench_catalog: threads=1, max_k=2 =====
[init] vertex counts: {"company": 1575, "forum_hasmoderator_person": 0, "university_islocatedin_city": 0, "person_likes_comment": 0, "tagclass_issubclassof_tagclass": 0, "comment_hastag_tag": 0, "city": 1343, "person_islocatedin_city": 0, "forum_hasmember_person": 0, "forum": 38501, "comment_islocatedin_country": 0, "tag": 16080, "comment_replyof_comment": 0, "forum_containerof_post": 0, "person_studyat_university": 0, "comment_hascreator_person": 0, "tagclass": 71, "country_ispartof_continent": 0, "post_islocatedin_country": 0, "continent": 6, "city_ispartof_country": 0, "person_likes_post": 0, "tag_hastype_tagclass": 0, "person": 3900, "person_workat_company": 0, "forum_hastag_tag": 0, "post": 410680, "country": 111, "comment_replyof_post": 0, "person_knows_person": 0, "post_hastag_tag": 0, "university": 6380, "person_hasinterest_tag": 0, "company_islocatedin_country": 0, "comment": 682797, "post_hascreator_person": 0} (2.367s)

--- Phase: 1-path degree (scan + count) ---
  comment_replyof_post: src=comment (682797 verts) 2.051s | dst=post (410680 verts) 0.918s | edges=340098
  comment_replyof_comment: src=comment (682797 verts) 2.015s | dst=comment (682797 verts) 0.808s | edges=342699
  person_knows_person: src=person (3900 verts) 0.464s | dst=person (3900 verts) 1.150s | edges=114190
  forum_containerof_post: src=forum (38501 verts) 0.787s | dst=post (410680 verts) 0.880s | edges=410680
  university_islocatedin_city: src=university (6380 verts) 0.078s | dst=city (1343 verts) 0.078s | edges=6380
  post_hastag_tag: src=post (410680 verts) 0.852s | dst=tag (16080 verts) 0.764s | edges=210665
  post_islocatedin_country: src=post (410680 verts) 0.823s | dst=country (111 verts) 0.706s | edges=410680
  forum_hastag_tag: src=forum (38501 verts) 0.799s | dst=tag (16080 verts) 0.752s | edges=124637
  comment_islocatedin_country: src=comment (682797 verts) 1.995s | dst=country (111 verts) 0.708s | edges=682797
  comment_hascreator_person: src=comment (682797 verts) 1.982s | dst=person (3900 verts) 1.149s | edges=682797
  comment_hastag_tag: src=comment (682797 verts) 1.975s | dst=tag (16080 verts) 0.773s | edges=799983
  person_islocatedin_city: src=person (3900 verts) 0.454s | dst=city (1343 verts) 0.079s | edges=3900
  person_studyat_university: src=person (3900 verts) 0.467s | dst=university (6380 verts) 0.075s | edges=3122
  country_ispartof_continent: src=country (111 verts) 0.070s | dst=continent (6 verts) 0.068s | edges=111
  city_ispartof_country: src=city (1343 verts) 0.071s | dst=country (111 verts) 0.706s | edges=1343
  company_islocatedin_country: src=company (1575 verts) 0.071s | dst=country (111 verts) 0.698s | edges=1575
  person_likes_post: src=person (3900 verts) 0.451s | dst=post (410680 verts) 0.916s | edges=230751
  forum_hasmember_person: src=forum (38501 verts) 0.820s | dst=person (3900 verts) 1.129s | edges=858523
  person_likes_comment: src=person (3900 verts) 0.455s | dst=comment (682797 verts) 0.828s | edges=405475
  tagclass_issubclassof_tagclass: src=tagclass (71 verts) 0.070s | dst=tagclass (71 verts) 0.083s | edges=70
  forum_hasmoderator_person: src=forum (38501 verts) 0.820s | dst=person (3900 verts) 1.121s | edges=38501
  post_hascreator_person: src=post (410680 verts) 0.824s | dst=person (3900 verts) 1.145s | edges=410680
  tag_hastype_tagclass: src=tag (16080 verts) 0.089s | dst=tagclass (71 verts) 0.078s | edges=16080
  person_workat_company: src=person (3900 verts) 0.463s | dst=company (1575 verts) 0.077s | edges=8265
  person_hasinterest_tag: src=person (3900 verts) 0.468s | dst=tag (16080 verts) 0.760s | edges=90036

--- Phase: 2-path degree (scan + extend lookup) ---
  comment -comment_islocatedin_country-> country -post_islocatedin_country-> post: left_extend (682797 verts) 2.019s | right_extend (410680 verts) 0.860s
  country -post_islocatedin_country-> post -forum_containerof_post-> forum: left_extend (111 verts) 0.722s | right_extend (38501 verts) 0.806s
  tagclass -tag_hastype_tagclass-> tag -tag_hastype_tagclass-> tagclass: left_extend (71 verts) 0.079s | right_extend (71 verts) 0.078s
  company -company_islocatedin_country-> country -company_islocatedin_country-> company: left_extend (1575 verts) 0.071s | right_extend (1575 verts) 0.071s
  forum -forum_hasmember_person-> person -person_likes_post-> post: left_extend (38501 verts) 0.838s | right_extend (410680 verts) 0.884s
  comment -person_likes_comment-> person -person_studyat_university-> university: left_extend (682797 verts) 0.813s | right_extend (6380 verts) 0.076s
  company -company_islocatedin_country-> country -post_islocatedin_country-> post: left_extend (1575 verts) 0.071s | right_extend (410680 verts) 0.867s
  continent -country_ispartof_continent-> country -country_ispartof_continent-> continent: left_extend (6 verts) 0.069s | right_extend (6 verts) 0.069s
  city -person_islocatedin_city-> person -person_workat_company-> company: left_extend (1343 verts) 0.079s | right_extend (1575 verts) 0.074s
  person -forum_hasmoderator_person-> forum -forum_containerof_post-> post: left_extend (3900 verts) 1.151s | right_extend (410680 verts) 0.905s
  forum -forum_hasmember_person-> person -person_studyat_university-> university: left_extend (38501 verts) 0.831s | right_extend (6380 verts) 0.073s
  person -person_knows_person-> person -person_hasinterest_tag-> tag: left_extend (3900 verts) 0.469s | right_extend (16080 verts) 0.797s
  city -city_ispartof_country-> country -comment_islocatedin_country-> comment: left_extend (1343 verts) 0.072s | right_extend (682797 verts) 2.063s
  post -post_hascreator_person-> person -person_hasinterest_tag-> tag: left_extend (410680 verts) 0.862s | right_extend (16080 verts) 0.763s
  company -company_islocatedin_country-> country -country_ispartof_continent-> continent: left_extend (1575 verts) 0.071s | right_extend (6 verts) 0.069s
  comment -comment_hascreator_person-> person -post_hascreator_person-> post: left_extend (682797 verts) 2.048s | right_extend (410680 verts) 0.852s
  forum -forum_hasmoderator_person-> person -post_hascreator_person-> post: left_extend (38501 verts) 0.811s | right_extend (410680 verts) 0.828s
  university -person_studyat_university-> person -person_studyat_university-> university: left_extend (6380 verts) 0.074s | right_extend (6380 verts) 0.074s
  city -person_islocatedin_city-> person -person_knows_person-> person: left_extend (1343 verts) 0.077s | right_extend (3900 verts) 0.476s
  person -forum_hasmember_person-> forum -forum_hasmember_person-> person: left_extend (3900 verts) 1.187s | right_extend (3900 verts) 1.152s
  person -forum_hasmoderator_person-> forum -forum_hastag_tag-> tag: left_extend (3900 verts) 1.167s | right_extend (16080 verts) 0.754s
  company -person_workat_company-> person -post_hascreator_person-> post: left_extend (1575 verts) 0.074s | right_extend (410680 verts) 0.858s
  comment -comment_hascreator_person-> person -person_studyat_university-> university: left_extend (682797 verts) 2.037s | right_extend (6380 verts) 0.076s
  company -person_workat_company-> person -person_knows_person-> person: left_extend (1575 verts) 0.076s | right_extend (3900 verts) 0.474s
  person -person_likes_post-> post -person_likes_post-> person: left_extend (3900 verts) 0.469s | right_extend (3900 verts) 0.481s
  forum -forum_hasmember_person-> person -forum_hasmember_person-> forum: left_extend (38501 verts) 0.818s | right_extend (38501 verts) 0.798s
  person -person_knows_person-> person -person_studyat_university-> university: left_extend (3900 verts) 0.439s | right_extend (6380 verts) 0.074s
  post -comment_replyof_post-> comment -comment_replyof_post-> post: left_extend (410680 verts) 0.933s | right_extend (410680 verts) 0.955s
  tag -tag_hastype_tagclass-> tagclass -tag_hastype_tagclass-> tag: left_extend (16080 verts) 0.091s | right_extend (16080 verts) 0.087s
  tag -tag_hastype_tagclass-> tagclass -tagclass_issubclassof_tagclass-> tagclass: left_extend (16080 verts) 0.087s | right_extend (71 verts) 0.068s
  forum -forum_hasmember_person-> person -person_hasinterest_tag-> tag: left_extend (38501 verts) 0.838s | right_extend (16080 verts) 0.748s
  company -person_workat_company-> person -forum_hasmember_person-> forum: left_extend (1575 verts) 0.074s | right_extend (38501 verts) 0.824s
  city -person_islocatedin_city-> person -forum_hasmember_person-> forum: left_extend (1343 verts) 0.077s | right_extend (38501 verts) 0.821s
  city -person_islocatedin_city-> person -person_likes_comment-> comment: left_extend (1343 verts) 0.076s | right_extend (682797 verts) 0.808s
  tagclass -tagclass_issubclassof_tagclass-> tagclass -tagclass_issubclassof_tagclass-> tagclass: left_extend (71 verts) 0.070s | right_extend (71 verts) 0.069s
  tag -person_hasinterest_tag-> person -person_studyat_university-> university: left_extend (16080 verts) 0.783s | right_extend (6380 verts) 0.075s
  forum -forum_containerof_post-> post -forum_containerof_post-> forum: left_extend (38501 verts) 0.827s | right_extend (38501 verts) 0.801s
  person -person_islocatedin_city-> city -person_islocatedin_city-> person: left_extend (3900 verts) 0.447s | right_extend (3900 verts) 0.435s
  person -person_likes_post-> post -post_hastag_tag-> tag: left_extend (3900 verts) 0.460s | right_extend (16080 verts) 0.766s
  post -person_likes_post-> person -person_hasinterest_tag-> tag: left_extend (410680 verts) 0.889s | right_extend (16080 verts) 0.778s
  comment -comment_hascreator_person-> person -person_likes_post-> post: left_extend (682797 verts) 2.032s | right_extend (410680 verts) 0.913s
  post -post_hastag_tag-> tag -tag_hastype_tagclass-> tagclass: left_extend (410680 verts) 0.861s | right_extend (71 verts) 0.083s
  country -comment_islocatedin_country-> comment -comment_hastag_tag-> tag: left_extend (111 verts) 0.747s | right_extend (16080 verts) 0.804s
  country -post_islocatedin_country-> post -person_likes_post-> person: left_extend (111 verts) 0.715s | right_extend (3900 verts) 0.461s
  comment -comment_replyof_post-> post -post_hastag_tag-> tag: left_extend (682797 verts) 2.090s | right_extend (16080 verts) 0.796s
  comment -comment_hastag_tag-> tag -forum_hastag_tag-> forum: left_extend (682797 verts) 1.994s | right_extend (38501 verts) 0.801s
  comment -comment_replyof_comment-> comment -comment_hascreator_person-> person: left_extend (682797 verts) 2.058s | right_extend (3900 verts) 1.182s
  country -city_ispartof_country-> city -person_islocatedin_city-> person: left_extend (111 verts) 0.688s | right_extend (3900 verts) 0.437s
  person -post_hascreator_person-> post -post_hastag_tag-> tag: left_extend (3900 verts) 1.147s | right_extend (16080 verts) 0.768s
  person -forum_hasmember_person-> forum -forum_hastag_tag-> tag: left_extend (3900 verts) 1.159s | right_extend (16080 verts) 0.755s
  person -person_studyat_university-> university -person_studyat_university-> person: left_extend (3900 verts) 0.454s | right_extend (3900 verts) 0.474s
  city -person_islocatedin_city-> person -person_islocatedin_city-> city: left_extend (1343 verts) 0.077s | right_extend (1343 verts) 0.076s
  post -comment_replyof_post-> comment -comment_hastag_tag-> tag: left_extend (410680 verts) 0.932s | right_extend (16080 verts) 0.813s
  post -post_hastag_tag-> tag -post_hastag_tag-> post: left_extend (410680 verts) 0.837s | right_extend (410680 verts) 0.855s
  comment -person_likes_comment-> person -person_likes_post-> post: left_extend (682797 verts) 0.816s | right_extend (410680 verts) 0.913s
  post -person_likes_post-> person -person_studyat_university-> university: left_extend (410680 verts) 0.920s | right_extend (6380 verts) 0.076s
  forum -forum_hasmember_person-> person -person_knows_person-> person: left_extend (38501 verts) 0.842s | right_extend (3900 verts) 0.442s
  person -forum_hasmember_person-> forum -forum_containerof_post-> post: left_extend (3900 verts) 1.161s | right_extend (410680 verts) 0.902s
  forum -forum_hasmoderator_person-> person -person_knows_person-> person: left_extend (38501 verts) 0.801s | right_extend (3900 verts) 0.441s
  post -person_likes_post-> person -post_hascreator_person-> post: left_extend (410680 verts) 0.900s | right_extend (410680 verts) 0.869s
  comment -comment_hascreator_person-> person -person_knows_person-> person: left_extend (682797 verts) 2.032s | right_extend (3900 verts) 0.462s
  comment -comment_replyof_comment-> comment -comment_hastag_tag-> tag: left_extend (682797 verts) 2.077s | right_extend (16080 verts) 0.814s
  person -forum_hasmoderator_person-> forum -forum_hasmoderator_person-> person: left_extend (3900 verts) 1.130s | right_extend (3900 verts) 1.110s
  comment -comment_hascreator_person-> person -person_workat_company-> company: left_extend (682797 verts) 2.016s | right_extend (1575 verts) 0.077s
  comment -person_likes_comment-> person -person_knows_person-> person: left_extend (682797 verts) 0.828s | right_extend (3900 verts) 0.465s
  person -comment_hascreator_person-> comment -comment_replyof_post-> post: left_extend (3900 verts) 1.223s | right_extend (410680 verts) 0.985s
  company -person_workat_company-> person -person_likes_post-> post: left_extend (1575 verts) 0.078s | right_extend (410680 verts) 0.963s
  comment -person_likes_comment-> person -forum_hasmoderator_person-> forum: left_extend (682797 verts) 0.826s | right_extend (38501 verts) 0.798s
  country -city_ispartof_country-> city -city_ispartof_country-> country: left_extend (111 verts) 0.681s | right_extend (111 verts) 0.676s
  country -post_islocatedin_country-> post -post_islocatedin_country-> country: left_extend (111 verts) 0.704s | right_extend (111 verts) 0.715s
  person -person_likes_post-> post -post_hascreator_person-> person: left_extend (3900 verts) 0.454s | right_extend (3900 verts) 1.154s
  person -person_workat_company-> company -person_workat_company-> person: left_extend (3900 verts) 0.454s | right_extend (3900 verts) 0.472s
  person -comment_hascreator_person-> comment -comment_hascreator_person-> person: left_extend (3900 verts) 1.181s | right_extend (3900 verts) 1.176s
  comment -comment_islocatedin_country-> country -company_islocatedin_country-> company: left_extend (682797 verts) 2.014s | right_extend (1575 verts) 0.073s
  country -comment_islocatedin_country-> comment -comment_replyof_post-> post: left_extend (111 verts) 0.756s | right_extend (410680 verts) 0.917s
  country -company_islocatedin_country-> company -company_islocatedin_country-> country: left_extend (111 verts) 0.704s | right_extend (111 verts) 0.685s
  comment -comment_replyof_comment-> comment -person_likes_comment-> person: left_extend (682797 verts) 2.040s | right_extend (3900 verts) 0.494s
  city -university_islocatedin_city-> university -person_studyat_university-> person: left_extend (1343 verts) 0.073s | right_extend (3900 verts) 0.481s
  forum -forum_hastag_tag-> tag -post_hastag_tag-> post: left_extend (38501 verts) 0.817s | right_extend (410680 verts) 0.822s
  tag -forum_hastag_tag-> forum -forum_hastag_tag-> tag: left_extend (16080 verts) 0.769s | right_extend (16080 verts) 0.750s
  comment -comment_hascreator_person-> person -comment_hascreator_person-> comment: left_extend (682797 verts) 2.024s | right_extend (682797 verts) 2.040s
  tag -post_hastag_tag-> post -post_hastag_tag-> tag: left_extend (16080 verts) 0.840s | right_extend (16080 verts) 0.814s
  city -person_islocatedin_city-> person -post_hascreator_person-> post: left_extend (1343 verts) 0.077s | right_extend (410680 verts) 0.857s
  tag -person_hasinterest_tag-> person -person_hasinterest_tag-> tag: left_extend (16080 verts) 0.774s | right_extend (16080 verts) 0.767s
  forum -forum_containerof_post-> post -post_hascreator_person-> person: left_extend (38501 verts) 0.810s | right_extend (3900 verts) 1.149s
  forum -forum_containerof_post-> post -person_likes_post-> person: left_extend (38501 verts) 0.836s | right_extend (3900 verts) 0.451s
  continent -country_ispartof_continent-> country -post_islocatedin_country-> post: left_extend (6 verts) 0.069s | right_extend (410680 verts) 0.852s
  city -city_ispartof_country-> country -post_islocatedin_country-> post: left_extend (1343 verts) 0.071s | right_extend (410680 verts) 0.868s
  person -person_hasinterest_tag-> tag -post_hastag_tag-> post: left_extend (3900 verts) 0.461s | right_extend (410680 verts) 0.863s
  comment -person_likes_comment-> person -post_hascreator_person-> post: left_extend (682797 verts) 0.819s | right_extend (410680 verts) 0.859s
  comment -comment_replyof_post-> post -post_hascreator_person-> person: left_extend (682797 verts) 2.059s | right_extend (3900 verts) 1.163s
  comment -comment_islocatedin_country-> country -country_ispartof_continent-> continent: left_extend (682797 verts) 2.016s | right_extend (6 verts) 0.069s
  post -post_islocatedin_country-> country -post_islocatedin_country-> post: left_extend (410680 verts) 0.869s | right_extend (410680 verts) 0.861s
  forum -forum_hasmoderator_person-> person -forum_hasmoderator_person-> forum: left_extend (38501 verts) 0.798s | right_extend (38501 verts) 0.777s
  forum -forum_containerof_post-> post -post_hastag_tag-> tag: left_extend (38501 verts) 0.803s | right_extend (16080 verts) 0.768s
  comment -comment_hascreator_person-> person -forum_hasmoderator_person-> forum: left_extend (682797 verts) 2.017s | right_extend (38501 verts) 0.802s
  city -city_ispartof_country-> country -city_ispartof_country-> city: left_extend (1343 verts) 0.070s | right_extend (1343 verts) 0.068s
  comment -comment_hascreator_person-> person -person_hasinterest_tag-> tag: left_extend (682797 verts) 2.048s | right_extend (16080 verts) 0.769s
  forum -forum_hastag_tag-> tag -forum_hastag_tag-> forum: left_extend (38501 verts) 0.792s | right_extend (38501 verts) 0.782s
  forum -forum_hasmember_person-> person -forum_hasmoderator_person-> forum: left_extend (38501 verts) 0.805s | right_extend (38501 verts) 0.832s
  city -person_islocatedin_city-> person -person_studyat_university-> university: left_extend (1343 verts) 0.077s | right_extend (6380 verts) 0.073s
  comment -comment_replyof_post-> post -forum_containerof_post-> forum: left_extend (682797 verts) 2.033s | right_extend (38501 verts) 0.822s
  person -person_knows_person-> person -person_knows_person-> person: left_extend (3900 verts) 0.447s | right_extend (3900 verts) 0.432s
  person -person_islocatedin_city-> city -university_islocatedin_city-> university: left_extend (3900 verts) 0.434s | right_extend (6380 verts) 0.078s
  post -person_likes_post-> person -person_likes_post-> post: left_extend (410680 verts) 0.905s | right_extend (410680 verts) 0.921s
  comment -person_likes_comment-> person -person_likes_comment-> comment: left_extend (682797 verts) 0.825s | right_extend (682797 verts) 0.821s
  country -post_islocatedin_country-> post -post_hastag_tag-> tag: left_extend (111 verts) 0.726s | right_extend (16080 verts) 0.769s
  person -person_knows_person-> person -person_likes_post-> post: left_extend (3900 verts) 0.459s | right_extend (410680 verts) 0.920s
  post -forum_containerof_post-> forum -forum_containerof_post-> post: left_extend (410680 verts) 0.939s | right_extend (410680 verts) 0.936s
  post -post_hascreator_person-> person -person_studyat_university-> university: left_extend (410680 verts) 0.856s | right_extend (6380 verts) 0.077s
  city -person_islocatedin_city-> person -forum_hasmoderator_person-> forum: left_extend (1343 verts) 0.079s | right_extend (38501 verts) 0.819s
  forum -forum_hasmember_person-> person -post_hascreator_person-> post: left_extend (38501 verts) 0.802s | right_extend (410680 verts) 0.824s
  post -post_hascreator_person-> person -post_hascreator_person-> post: left_extend (410680 verts) 0.864s | right_extend (410680 verts) 0.869s
  post -forum_containerof_post-> forum -forum_hastag_tag-> tag: left_extend (410680 verts) 0.934s | right_extend (16080 verts) 0.778s
  person -person_hasinterest_tag-> tag -tag_hastype_tagclass-> tagclass: left_extend (3900 verts) 0.459s | right_extend (71 verts) 0.083s
  person -post_hascreator_person-> post -post_hascreator_person-> person: left_extend (3900 verts) 1.176s | right_extend (3900 verts) 1.138s
  forum -forum_hastag_tag-> tag -tag_hastype_tagclass-> tagclass: left_extend (38501 verts) 0.796s | right_extend (71 verts) 0.079s
  city -person_islocatedin_city-> person -person_hasinterest_tag-> tag: left_extend (1343 verts) 0.077s | right_extend (16080 verts) 0.776s
  city -city_ispartof_country-> country -company_islocatedin_country-> company: left_extend (1343 verts) 0.071s | right_extend (1575 verts) 0.069s
  city -university_islocatedin_city-> university -university_islocatedin_city-> city: left_extend (1343 verts) 0.076s | right_extend (1343 verts) 0.076s
  forum -forum_hasmoderator_person-> person -person_studyat_university-> university: left_extend (38501 verts) 0.818s | right_extend (6380 verts) 0.074s
  university -university_islocatedin_city-> city -university_islocatedin_city-> university: left_extend (6380 verts) 0.077s | right_extend (6380 verts) 0.075s
  comment -comment_islocatedin_country-> country -comment_islocatedin_country-> comment: left_extend (682797 verts) 2.054s | right_extend (682797 verts) 2.023s
  person -person_likes_comment-> comment -comment_replyof_post-> post: left_extend (3900 verts) 0.478s | right_extend (410680 verts) 0.949s
  person -person_hasinterest_tag-> tag -person_hasinterest_tag-> person: left_extend (3900 verts) 0.467s | right_extend (3900 verts) 0.470s
  country -post_islocatedin_country-> post -post_hascreator_person-> person: left_extend (111 verts) 0.717s | right_extend (3900 verts) 1.149s
  forum -forum_hasmoderator_person-> person -person_likes_post-> post: left_extend (38501 verts) 0.788s | right_extend (410680 verts) 0.880s
  comment -comment_hascreator_person-> person -forum_hasmember_person-> forum: left_extend (682797 verts) 2.051s | right_extend (38501 verts) 0.817s
  country -company_islocatedin_country-> company -person_workat_company-> person: left_extend (111 verts) 0.690s | right_extend (3900 verts) 0.447s
  comment -comment_hastag_tag-> tag -person_hasinterest_tag-> person: left_extend (682797 verts) 1.991s | right_extend (3900 verts) 0.465s
  person -person_likes_comment-> comment -person_likes_comment-> person: left_extend (3900 verts) 0.487s | right_extend (3900 verts) 0.452s
  country -city_ispartof_country-> city -university_islocatedin_city-> university: left_extend (111 verts) 0.694s | right_extend (6380 verts) 0.078s
  comment -person_likes_comment-> person -forum_hasmember_person-> forum: left_extend (682797 verts) 0.805s | right_extend (38501 verts) 0.818s
  comment -person_likes_comment-> person -person_workat_company-> company: left_extend (682797 verts) 0.791s | right_extend (1575 verts) 0.077s
  company -person_workat_company-> person -person_workat_company-> company: left_extend (1575 verts) 0.077s | right_extend (1575 verts) 0.075s
  comment -comment_replyof_comment-> comment -comment_islocatedin_country-> country: left_extend (682797 verts) 2.076s | right_extend (111 verts) 0.740s
  comment -comment_replyof_post-> post -comment_replyof_post-> comment: left_extend (682797 verts) 2.027s | right_extend (682797 verts) 2.034s
  forum -forum_hastag_tag-> tag -person_hasinterest_tag-> person: left_extend (38501 verts) 0.804s | right_extend (3900 verts) 0.442s
  comment -comment_replyof_post-> post -post_islocatedin_country-> country: left_extend (682797 verts) 2.023s | right_extend (111 verts) 0.726s
  company -person_workat_company-> person -forum_hasmoderator_person-> forum: left_extend (1575 verts) 0.076s | right_extend (38501 verts) 0.798s
  comment -comment_hastag_tag-> tag -post_hastag_tag-> post: left_extend (682797 verts) 2.146s | right_extend (410680 verts) 0.862s
  person -comment_hascreator_person-> comment -person_likes_comment-> person: left_extend (3900 verts) 1.181s | right_extend (3900 verts) 0.467s
  person -person_likes_comment-> comment -comment_hastag_tag-> tag: left_extend (3900 verts) 0.470s | right_extend (16080 verts) 0.827s
  company -person_workat_company-> person -person_hasinterest_tag-> tag: left_extend (1575 verts) 0.075s | right_extend (16080 verts) 0.765s
  comment -person_likes_comment-> person -person_hasinterest_tag-> tag: left_extend (682797 verts) 0.795s | right_extend (16080 verts) 0.762s
  comment -comment_hastag_tag-> tag -comment_hastag_tag-> comment: left_extend (682797 verts) 2.010s | right_extend (682797 verts) 2.009s
  person -comment_hascreator_person-> comment -comment_hastag_tag-> tag: left_extend (3900 verts) 1.182s | right_extend (16080 verts) 0.802s
  comment -comment_hascreator_person-> person -person_likes_comment-> comment: left_extend (682797 verts) 2.027s | right_extend (682797 verts) 0.812s
  country -country_ispartof_continent-> continent -country_ispartof_continent-> country: left_extend (111 verts) 0.071s | right_extend (111 verts) 0.070s
  city -person_islocatedin_city-> person -person_likes_post-> post: left_extend (1343 verts) 0.078s | right_extend (410680 verts) 0.920s
  country -comment_islocatedin_country-> comment -comment_islocatedin_country-> country: left_extend (111 verts) 0.739s | right_extend (111 verts) 0.713s
  comment -comment_hastag_tag-> tag -tag_hastype_tagclass-> tagclass: left_extend (682797 verts) 1.986s | right_extend (71 verts) 0.080s
  forum -forum_hasmoderator_person-> person -person_hasinterest_tag-> tag: left_extend (38501 verts) 0.815s | right_extend (16080 verts) 0.753s
  person -person_knows_person-> person -post_hascreator_person-> post: left_extend (3900 verts) 0.457s | right_extend (410680 verts) 0.864s
  country -comment_islocatedin_country-> comment -comment_hascreator_person-> person: left_extend (111 verts) 0.742s | right_extend (3900 verts) 1.172s
  tag -comment_hastag_tag-> comment -comment_hastag_tag-> tag: left_extend (16080 verts) 0.800s | right_extend (16080 verts) 0.795s
  person -forum_hasmember_person-> forum -forum_hasmoderator_person-> person: left_extend (3900 verts) 1.174s | right_extend (3900 verts) 1.108s
  comment -comment_replyof_post-> post -person_likes_post-> person: left_extend (682797 verts) 2.020s | right_extend (3900 verts) 0.475s
  comment -comment_replyof_comment-> comment -comment_replyof_comment-> comment: left_extend (682797 verts) 2.071s | right_extend (682797 verts) 2.044s
  comment -comment_replyof_comment-> comment -comment_replyof_post-> post: left_extend (682797 verts) 2.041s | right_extend (410680 verts) 0.936s
  city -city_ispartof_country-> country -country_ispartof_continent-> continent: left_extend (1343 verts) 0.070s | right_extend (6 verts) 0.069s
  company -person_workat_company-> person -person_studyat_university-> university: left_extend (1575 verts) 0.075s | right_extend (6380 verts) 0.074s
  country -comment_islocatedin_country-> comment -person_likes_comment-> person: left_extend (111 verts) 0.750s | right_extend (3900 verts) 0.464s
  city -person_islocatedin_city-> person -comment_hascreator_person-> comment: left_extend (1343 verts) 0.078s | right_extend (682797 verts) 2.029s

===== bench_catalog done =====
minigu> 


minigu> call bench_catalog("ldbc", 2, 2);
===== bench_catalog: threads=2, max_k=2 =====
[init] vertex counts: {"city": 1343, "company_islocatedin_country": 0, "post_hascreator_person": 0, "post": 410680, "continent": 6, "tagclass_issubclassof_tagclass": 0, "comment_islocatedin_country": 0, "tag": 16080, "country_ispartof_continent": 0, "forum_hastag_tag": 0, "person_studyat_university": 0, "forum_containerof_post": 0, "comment_hascreator_person": 0, "person_hasinterest_tag": 0, "forum_hasmoderator_person": 0, "person": 3900, "post_islocatedin_country": 0, "tagclass": 71, "person_knows_person": 0, "city_ispartof_country": 0, "person_likes_comment": 0, "country": 111, "comment_replyof_post": 0, "comment_hastag_tag": 0, "company": 1575, "post_hastag_tag": 0, "tag_hastype_tagclass": 0, "university_islocatedin_city": 0, "forum_hasmember_person": 0, "person_likes_post": 0, "forum": 38501, "comment_replyof_comment": 0, "university": 6380, "comment": 682797, "person_islocatedin_city": 0, "person_workat_company": 0} (2.362s)

--- Phase: 1-path degree (scan + count) ---
  person_likes_comment: src=person (3900 verts) 0.285s | dst=comment (682797 verts) 0.462s | edges=405475
  person_workat_company: src=person (3900 verts) 0.266s | dst=company (1575 verts) 0.073s | edges=8265
  person_studyat_university: src=person (3900 verts) 0.264s | dst=university (6380 verts) 0.073s | edges=3122
  post_hastag_tag: src=post (410680 verts) 0.461s | dst=tag (16080 verts) 0.426s | edges=210665
  tagclass_issubclassof_tagclass: src=tagclass (71 verts) 0.070s | dst=tagclass (71 verts) 0.079s | edges=70
  university_islocatedin_city: src=university (6380 verts) 0.073s | dst=city (1343 verts) 0.073s | edges=6380
  comment_hastag_tag: src=comment (682797 verts) 1.055s | dst=tag (16080 verts) 0.429s | edges=799983
  comment_replyof_comment: src=comment (682797 verts) 1.054s | dst=comment (682797 verts) 0.458s | edges=342699
  person_knows_person: src=person (3900 verts) 0.267s | dst=person (3900 verts) 0.605s | edges=114190
  comment_islocatedin_country: src=comment (682797 verts) 1.050s | dst=country (111 verts) 0.416s | edges=682797
  post_hascreator_person: src=post (410680 verts) 0.457s | dst=person (3900 verts) 0.611s | edges=410680
  company_islocatedin_country: src=company (1575 verts) 0.072s | dst=country (111 verts) 0.419s | edges=1575
  city_ispartof_country: src=city (1343 verts) 0.070s | dst=country (111 verts) 0.411s | edges=1343
  person_islocatedin_city: src=person (3900 verts) 0.262s | dst=city (1343 verts) 0.075s | edges=3900
  tag_hastype_tagclass: src=tag (16080 verts) 0.081s | dst=tagclass (71 verts) 0.078s | edges=16080
  post_islocatedin_country: src=post (410680 verts) 0.464s | dst=country (111 verts) 0.417s | edges=410680
  forum_containerof_post: src=forum (38501 verts) 0.435s | dst=post (410680 verts) 0.485s | edges=410680
  person_likes_post: src=person (3900 verts) 0.268s | dst=post (410680 verts) 0.489s | edges=230751
  forum_hasmoderator_person: src=forum (38501 verts) 0.444s | dst=person (3900 verts) 0.602s | edges=38501
  person_hasinterest_tag: src=person (3900 verts) 0.269s | dst=tag (16080 verts) 0.426s | edges=90036
  forum_hasmember_person: src=forum (38501 verts) 0.452s | dst=person (3900 verts) 0.608s | edges=858523
  forum_hastag_tag: src=forum (38501 verts) 0.447s | dst=tag (16080 verts) 0.419s | edges=124637
  country_ispartof_continent: src=country (111 verts) 0.070s | dst=continent (6 verts) 0.070s | edges=111
  comment_hascreator_person: src=comment (682797 verts) 1.054s | dst=person (3900 verts) 0.615s | edges=682797
  comment_replyof_post: src=comment (682797 verts) 1.048s | dst=post (410680 verts) 0.494s | edges=340098

--- Phase: 2-path degree (scan + extend lookup) ---
  comment -person_likes_comment-> person -person_knows_person-> person: left_extend (682797 verts) 0.470s | right_extend (3900 verts) 0.267s
  comment -comment_hastag_tag-> tag -post_hastag_tag-> post: left_extend (682797 verts) 1.052s | right_extend (410680 verts) 0.467s
  person -person_studyat_university-> university -person_studyat_university-> person: left_extend (3900 verts) 0.267s | right_extend (3900 verts) 0.258s
  person -comment_hascreator_person-> comment -comment_hascreator_person-> person: left_extend (3900 verts) 0.628s | right_extend (3900 verts) 0.633s
  person -comment_hascreator_person-> comment -comment_hastag_tag-> tag: left_extend (3900 verts) 0.632s | right_extend (16080 verts) 0.452s
  company -person_workat_company-> person -post_hascreator_person-> post: left_extend (1575 verts) 0.075s | right_extend (410680 verts) 0.465s
  forum -forum_hasmoderator_person-> person -person_likes_post-> post: left_extend (38501 verts) 0.445s | right_extend (410680 verts) 0.487s
  comment -comment_hascreator_person-> person -person_studyat_university-> university: left_extend (682797 verts) 1.070s | right_extend (6380 verts) 0.071s
  company -person_workat_company-> person -forum_hasmember_person-> forum: left_extend (1575 verts) 0.073s | right_extend (38501 verts) 0.454s
  forum -forum_hasmoderator_person-> person -forum_hasmoderator_person-> forum: left_extend (38501 verts) 0.433s | right_extend (38501 verts) 0.433s
  company -person_workat_company-> person -person_studyat_university-> university: left_extend (1575 verts) 0.073s | right_extend (6380 verts) 0.072s
  post -post_hascreator_person-> person -post_hascreator_person-> post: left_extend (410680 verts) 0.467s | right_extend (410680 verts) 0.470s
  city -person_islocatedin_city-> person -post_hascreator_person-> post: left_extend (1343 verts) 0.074s | right_extend (410680 verts) 0.470s
  country -country_ispartof_continent-> continent -country_ispartof_continent-> country: left_extend (111 verts) 0.069s | right_extend (111 verts) 0.068s
  comment -person_likes_comment-> person -forum_hasmember_person-> forum: left_extend (682797 verts) 0.465s | right_extend (38501 verts) 0.452s
  person -person_islocatedin_city-> city -person_islocatedin_city-> person: left_extend (3900 verts) 0.261s | right_extend (3900 verts) 0.260s
  person -person_islocatedin_city-> city -university_islocatedin_city-> university: left_extend (3900 verts) 0.268s | right_extend (6380 verts) 0.074s
  person -person_likes_comment-> comment -comment_replyof_post-> post: left_extend (3900 verts) 0.277s | right_extend (410680 verts) 0.516s
  city -city_ispartof_country-> country -post_islocatedin_country-> post: left_extend (1343 verts) 0.070s | right_extend (410680 verts) 0.471s
  comment -comment_hastag_tag-> tag -person_hasinterest_tag-> person: left_extend (682797 verts) 1.059s | right_extend (3900 verts) 0.268s
  company -company_islocatedin_country-> country -company_islocatedin_country-> company: left_extend (1575 verts) 0.071s | right_extend (1575 verts) 0.070s
  comment -comment_hastag_tag-> tag -forum_hastag_tag-> forum: left_extend (682797 verts) 1.059s | right_extend (38501 verts) 0.443s
  post -post_hascreator_person-> person -person_studyat_university-> university: left_extend (410680 verts) 0.459s | right_extend (6380 verts) 0.073s
  country -city_ispartof_country-> city -person_islocatedin_city-> person: left_extend (111 verts) 0.414s | right_extend (3900 verts) 0.262s
  tag -tag_hastype_tagclass-> tagclass -tag_hastype_tagclass-> tag: left_extend (16080 verts) 0.078s | right_extend (16080 verts) 0.078s
  person -forum_hasmoderator_person-> forum -forum_containerof_post-> post: left_extend (3900 verts) 0.617s | right_extend (410680 verts) 0.510s
  city -university_islocatedin_city-> university -person_studyat_university-> person: left_extend (1343 verts) 0.075s | right_extend (3900 verts) 0.268s
  person -person_hasinterest_tag-> tag -post_hastag_tag-> post: left_extend (3900 verts) 0.268s | right_extend (410680 verts) 0.468s
  person -forum_hasmember_person-> forum -forum_hastag_tag-> tag: left_extend (3900 verts) 0.631s | right_extend (16080 verts) 0.428s
  comment -comment_islocatedin_country-> country -company_islocatedin_country-> company: left_extend (682797 verts) 1.068s | right_extend (1575 verts) 0.072s
  post -forum_containerof_post-> forum -forum_hastag_tag-> tag: left_extend (410680 verts) 0.505s | right_extend (16080 verts) 0.429s
  person -person_knows_person-> person -person_likes_post-> post: left_extend (3900 verts) 0.271s | right_extend (410680 verts) 0.497s
  comment -person_likes_comment-> person -person_studyat_university-> university: left_extend (682797 verts) 0.457s | right_extend (6380 verts) 0.072s
  country -comment_islocatedin_country-> comment -comment_replyof_post-> post: left_extend (111 verts) 0.435s | right_extend (410680 verts) 0.505s
  post -post_hastag_tag-> tag -post_hastag_tag-> post: left_extend (410680 verts) 0.467s | right_extend (410680 verts) 0.468s
  forum -forum_containerof_post-> post -person_likes_post-> person: left_extend (38501 verts) 0.453s | right_extend (3900 verts) 0.268s
  company -company_islocatedin_country-> country -post_islocatedin_country-> post: left_extend (1575 verts) 0.070s | right_extend (410680 verts) 0.468s
  person -person_hasinterest_tag-> tag -person_hasinterest_tag-> person: left_extend (3900 verts) 0.267s | right_extend (3900 verts) 0.259s
  country -city_ispartof_country-> city -city_ispartof_country-> country: left_extend (111 verts) 0.407s | right_extend (111 verts) 0.401s
  forum -forum_hastag_tag-> tag -forum_hastag_tag-> forum: left_extend (38501 verts) 0.434s | right_extend (38501 verts) 0.434s
  university -university_islocatedin_city-> city -university_islocatedin_city-> university: left_extend (6380 verts) 0.073s | right_extend (6380 verts) 0.072s
  comment -comment_replyof_comment-> comment -comment_replyof_comment-> comment: left_extend (682797 verts) 1.081s | right_extend (682797 verts) 1.089s
  forum -forum_hasmember_person-> person -forum_hasmoderator_person-> forum: left_extend (38501 verts) 0.454s | right_extend (38501 verts) 0.432s
  post -comment_replyof_post-> comment -comment_replyof_post-> post: left_extend (410680 verts) 0.502s | right_extend (410680 verts) 0.513s
  post -post_islocatedin_country-> country -post_islocatedin_country-> post: left_extend (410680 verts) 0.471s | right_extend (410680 verts) 0.472s
  post -person_likes_post-> person -person_likes_post-> post: left_extend (410680 verts) 0.500s | right_extend (410680 verts) 0.502s
  comment -comment_hascreator_person-> person -post_hascreator_person-> post: left_extend (682797 verts) 1.076s | right_extend (410680 verts) 0.469s
  post -post_hascreator_person-> person -person_hasinterest_tag-> tag: left_extend (410680 verts) 0.472s | right_extend (16080 verts) 0.428s
  city -person_islocatedin_city-> person -forum_hasmember_person-> forum: left_extend (1343 verts) 0.075s | right_extend (38501 verts) 0.456s
  city -city_ispartof_country-> country -country_ispartof_continent-> continent: left_extend (1343 verts) 0.071s | right_extend (6 verts) 0.069s
  person -person_hasinterest_tag-> tag -tag_hastype_tagclass-> tagclass: left_extend (3900 verts) 0.268s | right_extend (71 verts) 0.077s
  company -company_islocatedin_country-> country -country_ispartof_continent-> continent: left_extend (1575 verts) 0.071s | right_extend (6 verts) 0.069s
  forum -forum_containerof_post-> post -post_hascreator_person-> person: left_extend (38501 verts) 0.454s | right_extend (3900 verts) 0.618s
  comment -comment_replyof_comment-> comment -person_likes_comment-> person: left_extend (682797 verts) 1.081s | right_extend (3900 verts) 0.275s
  city -person_islocatedin_city-> person -person_knows_person-> person: left_extend (1343 verts) 0.074s | right_extend (3900 verts) 0.266s
  person -person_knows_person-> person -person_hasinterest_tag-> tag: left_extend (3900 verts) 0.260s | right_extend (16080 verts) 0.421s
  comment -comment_replyof_comment-> comment -comment_replyof_post-> post: left_extend (682797 verts) 1.079s | right_extend (410680 verts) 0.514s
  country -post_islocatedin_country-> post -forum_containerof_post-> forum: left_extend (111 verts) 0.426s | right_extend (38501 verts) 0.448s
  forum -forum_containerof_post-> post -forum_containerof_post-> forum: left_extend (38501 verts) 0.445s | right_extend (38501 verts) 0.444s
  forum -forum_hasmoderator_person-> person -post_hascreator_person-> post: left_extend (38501 verts) 0.433s | right_extend (410680 verts) 0.458s
  company -person_workat_company-> person -forum_hasmoderator_person-> forum: left_extend (1575 verts) 0.072s | right_extend (38501 verts) 0.445s
  forum -forum_hasmoderator_person-> person -person_hasinterest_tag-> tag: left_extend (38501 verts) 0.433s | right_extend (16080 verts) 0.426s
  comment -comment_hastag_tag-> tag -comment_hastag_tag-> comment: left_extend (682797 verts) 1.058s | right_extend (682797 verts) 1.061s
  tagclass -tag_hastype_tagclass-> tag -tag_hastype_tagclass-> tagclass: left_extend (71 verts) 0.080s | right_extend (71 verts) 0.079s
  forum -forum_hastag_tag-> tag -post_hastag_tag-> post: left_extend (38501 verts) 0.446s | right_extend (410680 verts) 0.457s
  forum -forum_hastag_tag-> tag -tag_hastype_tagclass-> tagclass: left_extend (38501 verts) 0.447s | right_extend (71 verts) 0.077s
  person -comment_hascreator_person-> comment -comment_replyof_post-> post: left_extend (3900 verts) 0.631s | right_extend (410680 verts) 0.513s
  tag -forum_hastag_tag-> forum -forum_hastag_tag-> tag: left_extend (16080 verts) 0.428s | right_extend (16080 verts) 0.428s
  post -person_likes_post-> person -post_hascreator_person-> post: left_extend (410680 verts) 0.502s | right_extend (410680 verts) 0.471s
  comment -comment_hascreator_person-> person -person_workat_company-> company: left_extend (682797 verts) 1.076s | right_extend (1575 verts) 0.074s
  comment -comment_hascreator_person-> person -forum_hasmoderator_person-> forum: left_extend (682797 verts) 1.074s | right_extend (38501 verts) 0.442s
  comment -person_likes_comment-> person -person_likes_comment-> comment: left_extend (682797 verts) 0.452s | right_extend (682797 verts) 0.461s
  comment -comment_replyof_post-> post -post_islocatedin_country-> country: left_extend (682797 verts) 1.071s | right_extend (111 verts) 0.430s
  comment -comment_replyof_post-> post -forum_containerof_post-> forum: left_extend (682797 verts) 1.064s | right_extend (38501 verts) 0.453s
  city -person_islocatedin_city-> person -person_likes_comment-> comment: left_extend (1343 verts) 0.072s | right_extend (682797 verts) 0.459s
  person -person_knows_person-> person -person_knows_person-> person: left_extend (3900 verts) 0.268s | right_extend (3900 verts) 0.260s
  forum -forum_hasmember_person-> person -person_knows_person-> person: left_extend (38501 verts) 0.446s | right_extend (3900 verts) 0.263s
  person -person_likes_post-> post -post_hascreator_person-> person: left_extend (3900 verts) 0.263s | right_extend (3900 verts) 0.618s
  person -forum_hasmember_person-> forum -forum_hasmember_person-> person: left_extend (3900 verts) 0.630s | right_extend (3900 verts) 0.632s
  person -forum_hasmoderator_person-> forum -forum_hasmoderator_person-> person: left_extend (3900 verts) 0.611s | right_extend (3900 verts) 0.609s
  tag -person_hasinterest_tag-> person -person_studyat_university-> university: left_extend (16080 verts) 0.429s | right_extend (6380 verts) 0.073s
  forum -forum_hasmember_person-> person -person_studyat_university-> university: left_extend (38501 verts) 0.455s | right_extend (6380 verts) 0.072s
  person -person_likes_comment-> comment -comment_hastag_tag-> tag: left_extend (3900 verts) 0.273s | right_extend (16080 verts) 0.452s
  forum -forum_hasmoderator_person-> person -person_knows_person-> person: left_extend (38501 verts) 0.443s | right_extend (3900 verts) 0.263s
  post -post_hastag_tag-> tag -tag_hastype_tagclass-> tagclass: left_extend (410680 verts) 0.469s | right_extend (71 verts) 0.079s
  comment -comment_hascreator_person-> person -forum_hasmember_person-> forum: left_extend (682797 verts) 1.074s | right_extend (38501 verts) 0.454s
  comment -comment_islocatedin_country-> country -post_islocatedin_country-> post: left_extend (682797 verts) 1.056s | right_extend (410680 verts) 0.468s
  comment -comment_hascreator_person-> person -comment_hascreator_person-> comment: left_extend (682797 verts) 1.073s | right_extend (682797 verts) 1.071s
  forum -forum_hasmember_person-> person -person_likes_post-> post: left_extend (38501 verts) 0.452s | right_extend (410680 verts) 0.488s
  tag -tag_hastype_tagclass-> tagclass -tagclass_issubclassof_tagclass-> tagclass: left_extend (16080 verts) 0.080s | right_extend (71 verts) 0.068s
  country -company_islocatedin_country-> company -company_islocatedin_country-> country: left_extend (111 verts) 0.416s | right_extend (111 verts) 0.404s
  country -company_islocatedin_country-> company -person_workat_company-> person: left_extend (111 verts) 0.402s | right_extend (3900 verts) 0.264s
  country -post_islocatedin_country-> post -person_likes_post-> person: left_extend (111 verts) 0.420s | right_extend (3900 verts) 0.268s
  forum -forum_hasmoderator_person-> person -person_studyat_university-> university: left_extend (38501 verts) 0.435s | right_extend (6380 verts) 0.072s
  comment -person_likes_comment-> person -person_likes_post-> post: left_extend (682797 verts) 0.457s | right_extend (410680 verts) 0.501s
  comment -comment_hastag_tag-> tag -tag_hastype_tagclass-> tagclass: left_extend (682797 verts) 1.058s | right_extend (71 verts) 0.079s
  city -person_islocatedin_city-> person -person_likes_post-> post: left_extend (1343 verts) 0.075s | right_extend (410680 verts) 0.502s
  post -person_likes_post-> person -person_hasinterest_tag-> tag: left_extend (410680 verts) 0.501s | right_extend (16080 verts) 0.429s
  forum -forum_hastag_tag-> tag -person_hasinterest_tag-> person: left_extend (38501 verts) 0.447s | right_extend (3900 verts) 0.264s
  tag -post_hastag_tag-> post -post_hastag_tag-> tag: left_extend (16080 verts) 0.429s | right_extend (16080 verts) 0.435s
  comment -comment_hascreator_person-> person -person_hasinterest_tag-> tag: left_extend (682797 verts) 1.075s | right_extend (16080 verts) 0.427s
  city -university_islocatedin_city-> university -university_islocatedin_city-> city: left_extend (1343 verts) 0.076s | right_extend (1343 verts) 0.073s
  person -post_hascreator_person-> post -post_hastag_tag-> tag: left_extend (3900 verts) 0.622s | right_extend (16080 verts) 0.436s
  person -person_workat_company-> company -person_workat_company-> person: left_extend (3900 verts) 0.268s | right_extend (3900 verts) 0.270s
  city -city_ispartof_country-> country -city_ispartof_country-> city: left_extend (1343 verts) 0.071s | right_extend (1343 verts) 0.070s
  forum -forum_hasmember_person-> person -person_hasinterest_tag-> tag: left_extend (38501 verts) 0.457s | right_extend (16080 verts) 0.420s
  country -post_islocatedin_country-> post -post_hastag_tag-> tag: left_extend (111 verts) 0.426s | right_extend (16080 verts) 0.426s
  country -comment_islocatedin_country-> comment -person_likes_comment-> person: left_extend (111 verts) 0.435s | right_extend (3900 verts) 0.272s
  comment -comment_replyof_post-> post -post_hascreator_person-> person: left_extend (682797 verts) 1.070s | right_extend (3900 verts) 0.622s
  country -comment_islocatedin_country-> comment -comment_islocatedin_country-> country: left_extend (111 verts) 0.436s | right_extend (111 verts) 0.428s
  person -person_likes_post-> post -post_hastag_tag-> tag: left_extend (3900 verts) 0.267s | right_extend (16080 verts) 0.432s
  company -person_workat_company-> person -person_hasinterest_tag-> tag: left_extend (1575 verts) 0.074s | right_extend (16080 verts) 0.427s
  person -forum_hasmember_person-> forum -forum_hasmoderator_person-> person: left_extend (3900 verts) 0.632s | right_extend (3900 verts) 0.612s
  comment -comment_replyof_comment-> comment -comment_hascreator_person-> person: left_extend (682797 verts) 1.075s | right_extend (3900 verts) 0.632s
  country -comment_islocatedin_country-> comment -comment_hascreator_person-> person: left_extend (111 verts) 0.436s | right_extend (3900 verts) 0.628s
  person -person_knows_person-> person -post_hascreator_person-> post: left_extend (3900 verts) 0.271s | right_extend (410680 verts) 0.472s
  person -comment_hascreator_person-> comment -person_likes_comment-> person: left_extend (3900 verts) 0.634s | right_extend (3900 verts) 0.275s
  city -person_islocatedin_city-> person -comment_hascreator_person-> comment: left_extend (1343 verts) 0.075s | right_extend (682797 verts) 1.074s
  city -person_islocatedin_city-> person -person_islocatedin_city-> city: left_extend (1343 verts) 0.076s | right_extend (1343 verts) 0.073s
  comment -comment_hascreator_person-> person -person_likes_post-> post: left_extend (682797 verts) 1.074s | right_extend (410680 verts) 0.503s
  comment -comment_replyof_post-> post -person_likes_post-> person: left_extend (682797 verts) 1.073s | right_extend (3900 verts) 0.271s
  city -person_islocatedin_city-> person -person_workat_company-> company: left_extend (1343 verts) 0.074s | right_extend (1575 verts) 0.073s
  company -person_workat_company-> person -person_likes_post-> post: left_extend (1575 verts) 0.072s | right_extend (410680 verts) 0.493s
  country -comment_islocatedin_country-> comment -comment_hastag_tag-> tag: left_extend (111 verts) 0.450s | right_extend (16080 verts) 0.448s
  post -comment_replyof_post-> comment -comment_hastag_tag-> tag: left_extend (410680 verts) 0.511s | right_extend (16080 verts) 0.452s
  comment -person_likes_comment-> person -person_workat_company-> company: left_extend (682797 verts) 0.457s | right_extend (1575 verts) 0.074s
  comment -comment_islocatedin_country-> country -country_ispartof_continent-> continent: left_extend (682797 verts) 1.070s | right_extend (6 verts) 0.070s
  city -person_islocatedin_city-> person -person_hasinterest_tag-> tag: left_extend (1343 verts) 0.074s | right_extend (16080 verts) 0.426s
  comment -comment_hascreator_person-> person -person_knows_person-> person: left_extend (682797 verts) 1.073s | right_extend (3900 verts) 0.267s
  forum -forum_containerof_post-> post -post_hastag_tag-> tag: left_extend (38501 verts) 0.458s | right_extend (16080 verts) 0.505s
  city -person_islocatedin_city-> person -person_studyat_university-> university: left_extend (1343 verts) 0.076s | right_extend (6380 verts) 0.073s
  comment -comment_islocatedin_country-> country -comment_islocatedin_country-> comment: left_extend (682797 verts) 1.137s | right_extend (682797 verts) 1.118s
  comment -comment_hascreator_person-> person -person_likes_comment-> comment: left_extend (682797 verts) 1.105s | right_extend (682797 verts) 0.468s
  continent -country_ispartof_continent-> country -post_islocatedin_country-> post: left_extend (6 verts) 0.070s | right_extend (410680 verts) 0.490s
  comment -person_likes_comment-> person -forum_hasmoderator_person-> forum: left_extend (682797 verts) 0.477s | right_extend (38501 verts) 0.450s
  country -city_ispartof_country-> city -university_islocatedin_city-> university: left_extend (111 verts) 0.415s | right_extend (6380 verts) 0.074s
  forum -forum_hasmember_person-> person -post_hascreator_person-> post: left_extend (38501 verts) 0.462s | right_extend (410680 verts) 0.476s
  person -forum_hasmoderator_person-> forum -forum_hastag_tag-> tag: left_extend (3900 verts) 0.613s | right_extend (16080 verts) 0.428s
  comment -comment_replyof_post-> post -comment_replyof_post-> comment: left_extend (682797 verts) 1.070s | right_extend (682797 verts) 1.072s
  person -forum_hasmember_person-> forum -forum_containerof_post-> post: left_extend (3900 verts) 0.633s | right_extend (410680 verts) 0.516s
  post -person_likes_post-> person -person_studyat_university-> university: left_extend (410680 verts) 0.508s | right_extend (6380 verts) 0.072s
  university -person_studyat_university-> person -person_studyat_university-> university: left_extend (6380 verts) 0.072s | right_extend (6380 verts) 0.071s
  comment -comment_replyof_comment-> comment -comment_islocatedin_country-> country: left_extend (682797 verts) 1.101s | right_extend (111 verts) 0.438s
  tagclass -tagclass_issubclassof_tagclass-> tagclass -tagclass_issubclassof_tagclass-> tagclass: left_extend (71 verts) 0.069s | right_extend (71 verts) 0.069s
  city -person_islocatedin_city-> person -forum_hasmoderator_person-> forum: left_extend (1343 verts) 0.074s | right_extend (38501 verts) 0.446s
  country -post_islocatedin_country-> post -post_islocatedin_country-> country: left_extend (111 verts) 0.419s | right_extend (111 verts) 0.426s
  person -person_likes_post-> post -person_likes_post-> person: left_extend (3900 verts) 0.268s | right_extend (3900 verts) 0.264s
  city -city_ispartof_country-> country -comment_islocatedin_country-> comment: left_extend (1343 verts) 0.070s | right_extend (682797 verts) 1.075s
  tag -comment_hastag_tag-> comment -comment_hastag_tag-> tag: left_extend (16080 verts) 0.452s | right_extend (16080 verts) 0.451s
  person -person_likes_comment-> comment -person_likes_comment-> person: left_extend (3900 verts) 0.276s | right_extend (3900 verts) 0.274s
  person -post_hascreator_person-> post -post_hascreator_person-> person: left_extend (3900 verts) 0.625s | right_extend (3900 verts) 0.625s
  comment -comment_replyof_comment-> comment -comment_hastag_tag-> tag: left_extend (682797 verts) 1.077s | right_extend (16080 verts) 0.452s
  company -person_workat_company-> person -person_workat_company-> company: left_extend (1575 verts) 0.074s | right_extend (1575 verts) 0.073s
  tag -person_hasinterest_tag-> person -person_hasinterest_tag-> tag: left_extend (16080 verts) 0.427s | right_extend (16080 verts) 0.432s
  continent -country_ispartof_continent-> country -country_ispartof_continent-> continent: left_extend (6 verts) 0.069s | right_extend (6 verts) 0.069s
  company -person_workat_company-> person -person_knows_person-> person: left_extend (1575 verts) 0.071s | right_extend (3900 verts) 0.269s
  person -person_knows_person-> person -person_studyat_university-> university: left_extend (3900 verts) 0.270s | right_extend (6380 verts) 0.073s
  city -city_ispartof_country-> country -company_islocatedin_country-> company: left_extend (1343 verts) 0.071s | right_extend (1575 verts) 0.069s
  comment -comment_replyof_post-> post -post_hastag_tag-> tag: left_extend (682797 verts) 1.074s | right_extend (16080 verts) 0.433s
  comment -person_likes_comment-> person -post_hascreator_person-> post: left_extend (682797 verts) 0.456s | right_extend (410680 verts) 0.466s
  forum -forum_hasmember_person-> person -forum_hasmember_person-> forum: left_extend (38501 verts) 0.457s | right_extend (38501 verts) 0.445s
  post -forum_containerof_post-> forum -forum_containerof_post-> post: left_extend (410680 verts) 0.499s | right_extend (410680 verts) 0.512s
  comment -person_likes_comment-> person -person_hasinterest_tag-> tag: left_extend (682797 verts) 0.462s | right_extend (16080 verts) 0.426s
  country -post_islocatedin_country-> post -post_hascreator_person-> person: left_extend (111 verts) 0.428s | right_extend (3900 verts) 0.616s

===== bench_catalog done =====
minigu> 




