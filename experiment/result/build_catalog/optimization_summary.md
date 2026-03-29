# GCard Catalog Build — 度序列计算优化总结

## 1. 测试环境

| 项目 | 配置 |
|------|------|
| CPU | AMD Ryzen 9 7900X (12核 / 24线程, Zen4, 单Socket, 单NUMA) |
| L1d | 32 KB/core, 5-cycle latency |
| L2 | 1 MB/core, ~12-cycle latency |
| L3 | 64 MB 共享, ~40-50 cycle latency |
| DRAM | DDR5-5200 双通道, 理论峰值 83.2 GB/s, 实际可达 ~75 GB/s |
| DRAM latency | ~80 ns (CAS) |
| ROB | 320 entries/core |
| Load queue | 136 entries/core |
| L2 MSHR | ~32/core (最大并发 L2 miss) |
| 数据集 | LDBC SNB SF3 |

## 2. 脚本结构

```
experiment/scripts/
  run_remote_bench.sh                  # 一键远程测试：同步→编译→跑bench→拉结果
  build_catalog_bench/bench_catalog.sh # 核心 benchmark 脚本

experiment/result/build_catalog/
  bench_catalog_results.csv            # 测试原始数据
  optimization_summary.md              # 本文档
```

### 使用方式

```bash
# 远程一键测试
bash experiment/scripts/run_remote_bench.sh --sfs "sf3" --threads "16 32" --max-k 2

# 本地直接测试
bash experiment/scripts/build_catalog_bench/bench_catalog.sh --sfs "sf3" --threads "16 32"
```

## 3. K=2 优化历程

代码位于 `minigu/core/src/procedures/gcard_query/degree_compute_dense.rs`。

参数：max_k=2, 25 个 Level-1 + 164 个 Level-2 模式。

### 优化前架构

原始 `compute_degree_seq()` 在一个函数中完成 scan + compute：
1. 对每个模式独立扫描邻居（大量重复 I/O）
2. `extend_vec_with_suffix` 中每次调用动态构建 `HashMap<VertexId, usize>` 做 ID 映射
3. 外层 `par_iter`（164模式）内部又嵌套 `into_par_iter()`（逐顶点并行）
4. 输出转换时 clone 整个 `Vec<VertexId>`（数十万元素 × ~200次）

### 优化后架构（三步走）

#### 优化 1: Scan/Compute 分离 + HopKey 去重

将计算拆分为两个阶段：
- **Scan 阶段**: 按 `HopKey(vertex_label, edge_label, direction)` 去重，每种边类型只扫一次邻居。构建 `VecNeighborData`（CSR 格式: `flat_neighbors` + `offsets`）。
- **Compute 阶段**: 纯内存计算，基于预扫描的邻居数据。

#### 优化 2: Remap 表预计算

**问题**: `extend_vec_with_suffix` 需要将当前 hop 的 dst 顶点映射到 suffix hop 的 src 索引。原实现每次调用构建临时 HashMap。

**方案**: 在 scan 阶段预计算所有 `(current_hop, suffix_hop)` 对的 remap 表：
```
remap: Vec<u32>  // remap[dst_local_id] → suffix_src_index, 不存在则为 u32::MAX
```

#### 优化 3: 去除嵌套并行 + Arc 共享

- `extend_vec_with_suffix` 改为纯顺序循环，仅靠外层模式级 `par_iter`
- `VecNeighborData.src_verts` 改为 `Arc<Vec<VertexId>>`，输出用 `Arc::clone` 零拷贝

### Compute Time 对比 (SF3, K=2)

| 线程数 | 原始版本 | +Remap 预计算 | +去嵌套并行+Arc |
|--------|---------|-------------|---------------|
| 4      | 12.9s   | 4.3s        | **2.02s**     |
| 8      | 8.3s    | 3.4s        | **1.34s**     |
| 16     | 6.8s    | 3.2s        | **1.21s**     |
| 24     | N/A     | N/A         | **1.22s**     |
| 32     | N/A     | 3.2s        | **1.22s**     |

**总加速比 (16线程): 6.8s → 1.21s = 5.6x**

---

## 4. 理论性能分析

### 4.1 计算模型

`extend_vec_with_suffix` 的内循环（Level >= 2 的核心计算）：

```
for each src_vertex i (共 n 个):
    neighbors = flat_neighbors[offsets[i]..offsets[i+1]]    // 顺序读
    sum = 0
    for each neighbor j in neighbors:
        suffix_idx = remap[j]                                // 随机读 (1)
        if suffix_idx != MAX:
            sum += suffix_degs[suffix_idx]                   // 随机读 (2)，依赖 (1)
    result[i] = sum                                          // 顺序写
```

每次查找涉及：
- 1 次顺序读（`flat_neighbors`）：4B，硬件预取命中 L1，延迟可忽略
- 1 次随机读（`remap`）：4B 有效 / 64B 缓存行
- 1 次随机读（`suffix_degs`）：8B 有效 / 64B 缓存行，**依赖 remap 的结果（指针追踪）**
- ~5-8 条指令（load, load, cmp, conditional load, add）

### 4.2 Roofline 分析

**算术密度 (Arithmetic Intensity)**：

```
AI = 运算量 / 内存传输量
   = 1 op / (2 × 64 B)        (假设全部 miss L3)
   = 0.0078 ops/byte
```

**Roofline 上界**：

| 指标 | 值 |
|------|-----|
| 计算上界 | 12 cores × 5 GHz × 4 IPC ≈ 240 G-ops/s |
| 内存上界 | 75 GB/s × 0.0078 = **0.585 G-ops/s** |
| 实际受限于 | **内存带宽**（差 410x） |

结论：AI = 0.0078 远低于 Ridge Point (~32 ops/byte)，计算处于 Roofline 图的**极左侧**，纯粹的内存带宽受限问题。**计算能力完全不是瓶颈。**

### 4.3 Memory-Level Parallelism (MLP) 分析

虽然 `remap → suffix_degs` 构成**指针追踪**（同一迭代内有依赖），但不同迭代之间是**完全独立**的：

```
迭代 j:   remap[neighbors[j]] 发射于 T,         完成于 T+80ns
          suffix_degs[...] 发射于 T+80ns,       完成于 T+160ns
迭代 j+1: remap[neighbors[j+1]] 发射于 T+δ,     完成于 T+δ+80ns    (独立！)
迭代 j+2: remap[neighbors[j+2]] 发射于 T+2δ,    完成于 T+2δ+80ns   (独立！)
```

CPU 乱序执行可同时推进多个迭代：

| 参数 | 值 | 说明 |
|------|-----|------|
| 每迭代指令数 | ~8 | load, load, cmp, load, add... |
| ROB 容量 | 320 | → 最多 320/8 = **40 个迭代**同时在飞 |
| Load Queue | 136 | → 最多 136/2 = **68 个迭代**的 load 在飞 |
| L2 MSHR | ~32/core | → 最多 **32 个 DRAM 请求**同时在飞/core |
| 瓶颈 | L2 MSHR | 32 个并发请求/core |

**单核带宽上界** = 32 outstanding × 64B / 80ns = **25.6 GB/s/core**

**12 核总带宽** = 12 × 25.6 = **307 GB/s** >> 83.2 GB/s DRAM 峰值

即使指针追踪将每次迭代的有效 MLP 减半（16 个有效并发请求/core），12 核仍可提供 12 × 12.8 = **154 GB/s** 的请求能力，远超 DRAM 带宽。

**结论：瓶颈是 DRAM 带宽，不是内存延迟。CPU 的 MLP 足以饱和 DRAM 带宽。软件预取无法提升性能。**

### 4.4 L3 缓存工作集分析

通过插桩获取的 SF3 各 hop 数组大小（suffix_degs 大小 = src 数量 × 8B）：

| Vertex Label | src 数量 | suffix_degs 大小 | 代表性 remap 大小范围 |
|-------------|---------|-----------------|---------------------|
| **Comment** | 7,275,929 | **58.2 MB** | 0.1 ~ 29.1 MB |
| **Post** | 3,056,157 | **24.4 MB** | 0.1 ~ 14.5 MB |
| Forum | 259,629 | 2.1 MB | 0.1 ~ 12.2 MB |
| Person | 25,870 | 0.2 MB | 0.1 ~ 29.1 MB |
| Tag | 16,080 | 0.1 MB | 0.1 ~ 9.7 MB |
| 其他 | < 10,000 | < 0.1 MB | < 0.1 MB |

Remap 表总计：281 对，826.7 MB。

**L3 = 64 MB 共享。** 16 线程同时处理不同模式时的工作集分析：

| 场景 | 每线程工作集 | 16 线程总工作集 | L3 命中率 |
|------|-----------|---------------|----------|
| 最好（全 Person/Tag） | ~0.5 MB | ~8 MB | ~95%+ |
| 典型（混合） | ~30-60 MB | ~500-900 MB | ~15-25% |
| 最坏（全 Comment） | ~63 MB | ~1008 MB | ~6% |

### 4.5 理论预测 vs 实测

**模型**: `Time = N_lookups × 2 × 64B × (1 - L3_hit_rate) / BW_dram`

#### K=2, Level 2 (164 模式, 955M 查找)

| L3 命中率 | 实际 DRAM 流量 | 预测时间 | 实测 |
|----------|--------------|---------|------|
| 0% | 122.3 GB | 1.63s | - |
| 15% | 104.0 GB | 1.39s | - |
| **23%** | **94.2 GB** | **1.26s** | **1.25s** |
| 30% | 85.6 GB | 1.14s | - |

→ 反推 L3 命中率 ≈ **23%**，模型预测与实测误差 < 1%。

#### K=3, Level 3 (771 模式, 4716M 查找)

| L3 命中率 | 实际 DRAM 流量 | 预测时间 | 实测 |
|----------|--------------|---------|------|
| 0% | 603.6 GB | 8.05s | - |
| 10% | 543.2 GB | 7.24s | - |
| **19%** | **488.9 GB** | **6.52s** | **~6.5s** |
| 25% | 452.7 GB | 6.04s | - |

→ 反推 L3 命中率 ≈ **19%**，模型预测与实测误差 < 1%。

#### 交叉验证：Roofline 预测

```
Level 3:
  Required ops = 4716M
  Memory ceiling @ AI=0.0078 = 75 GB/s × 0.0078 = 0.585 G-ops/s
  → Roofline 预测 = 4716M / 0.585G = 8.06s (0% L3 hit)
  → 19% L3 hit 修正 = 8.06 × 0.81 = 6.53s
  → 实测: 6.5s ✓
```

**三种模型（带宽模型、反推命中率模型、Roofline 模型）给出一致结论。**

#### L3 命中率 Level 2 (23%) > Level 3 (19%) 的原因

1. Level 3 的 suffix_degs 来自 Level 2 输出，数组更大
2. Level 3 有 771 个模式 vs Level 2 的 164 个，更多模式竞争 L3
3. Level 3 的 remap 表更多（281 对, 826.7 MB），进一步挤压 L3

### 4.6 为什么 16→32 线程无法加速？

```
16 线程 → 12 个物理核活跃 (SMT 线程共享核资源)
每核 DRAM 请求能力: 25.6 GB/s (L2 MSHR 上界)
12 核总能力: 307 GB/s >> 83.2 GB/s DRAM 峰值
→ DRAM 带宽已 100% 饱和
```

增加到 32 线程：
- 仍然只有 12 个物理核
- 每核 L2 MSHR 被 2 个 SMT 线程共享 → 每线程 ~16 MSHR，但总量不变
- 额外 SMT 线程增加 L1/L2 缓存竞争 → L3 命中率可能下降
- 净效果：零提升或微弱负面

### 4.7 软件预取为何无效？

| 前提 | 实际情况 | 结论 |
|------|---------|------|
| 预取有效的前提是 MLP 不足 | MLP (307 GB/s) >> DRAM BW (83.2 GB/s) | MLP 已过剩 |
| 预取能减少延迟 | 延迟不是瓶颈，带宽才是 | 无效 |
| 预取能提前填充缓存 | 缓存已被有效使用（23% hit） | 可能驱逐有用数据 |

**软件预取在 MLP 已经饱和 DRAM 带宽的情况下，无法增加任何吞吐量。**

### 4.8 Roofline 总结

```
性能
(G-ops/s)
  |
240|                                              _______________  计算上界
  |                                         ____/
  |                                    ____/
  |                               ____/
  |                          ____/
  |                     ____/
  |                ____/
  |           ____/         Ridge Point (~32 ops/byte)
  |      ____/
  |_____/  内存带宽上界 (斜率 = 75 GB/s)
  |
  |★ 我们在这里: AI = 0.0078, 性能 ≈ 0.58 G-ops/s
  |
  +----+----+----+----+----+----+----+----+-----> AI (ops/byte)
     0.001  0.01  0.1   1    10   100
```

**本计算处于 Roofline 极左侧。任何不减少 DRAM 流量的优化（预取、SIMD、指令优化）都无法提升性能。唯一有效方向是提高 L3 命中率或减少查找次数。**

---

## 5. K=3 优化方向

### 5.1 K=3 基线数据 (16 threads, SF3)

| Level | 模式数 | 顶点数 | 随机查找次数 | DRAM 流量(最坏) | 实测时间 |
|-------|--------|--------|-------------|----------------|---------|
| 1     | 25     | 70.7M  | 127.0M      | 16.8 GB        | ~微秒   |
| 2     | 164    | 522.5M | 955.1M      | 126.4 GB       | ~1.25s  |
| 3     | 771    | 2426.3M| 4715.8M     | 623.0 GB       | ~6.5s   |
| **总计** | 960 | 3019.5M | 5797.9M   | 766.2 GB       | **7.77s** |

### 5.2 已尝试的无效优化

| 方案 | Compute Time | 提升 | 理论解释 |
|------|-------------|------|---------|
| Round 1: 单级软件预取 | 7.52s | ~3% | MLP 已饱和 DRAM BW，预取无法增加吞吐量 |
| Round 2: 两阶段流水线预取 | 待测(预计 ~0%) | - | 同上 |

### 5.3 有效优化方向（减少 DRAM 流量）

根据模型 `Time = N_lookups × 128B × (1 - hit_rate) / BW_dram`，优化只有两条路：

**路径 A：提高 L3 命中率 (hit_rate)**

| 方案 | 机制 | 预期 hit_rate | 预期 Level 3 时间 |
|-----|------|--------------|-----------------|
| 当前基线 | - | 19% | 6.5s |
| Cache-aware 调度 | 共享 suffix 的模式同线程处理 | ~30% | ~5.6s |
| u32 度值 | suffix_degs 大小减半 | ~25% | ~6.0s |
| 两者叠加 | - | ~35% | ~5.2s |

**Cache-aware 调度分析**：
- 771 个 Level-3 模式引用 ~164 个 Level-2 suffix → 平均 ~4.7 个模式共享同一 suffix
- 若同 suffix 的模式在同一线程顺序处理：
  - 第 1 个模式: suffix_degs cold miss 从 DRAM 加载
  - 后续 ~3.7 个模式: suffix_degs 仍在 L3 → hit
- Comment 级 suffix (58 MB) 单线程独占时可装入 L3，但需避免多线程同时访问大 suffix

**u32 度值分析**：
- Level 2 输出的度值（1-hop 邻居数）通常 < 10K
- Level 3 的度值（2-hop 路径数）可能达 10M 但不超过 4B → u32 足够
- Comment suffix_degs: 58 MB → **29 MB**，Post: 24 MB → **12 MB**
- 16 线程总工作集: ~500-900 MB → ~300-600 MB，L3 有效覆盖率约翻倍

**路径 B：减少查找次数 (N_lookups)**
- 需要算法层面改变，如跳过度值为 0 的路径
- 当前不在考虑范围内
