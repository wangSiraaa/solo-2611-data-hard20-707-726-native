# coilcut — 一维卷材分切几何服务与余料/交付台账

面向合成一维卷材的独立分切几何服务 + SQLite 库存/交付台账。只处理一维切割，
不做自动排版、二维切割或采购。长度可用整数或精确有理数（`"3/4"` 或 `1/3`），
全程用 `fractions.Fraction` 计算，无浮点误差。

材料目录 `materials/731/` 提供了初件、刀缝、实际操作和交付标签读取方；本实现
补上了缺失的分切实现，并让交付物真实通过 `materials/731/delivery_reader.py`
读取。

## 快速开始

```bash
# 1) 几何演示（无数据库）：展示翻面后第二刀落在根轴哪里
python3 -m coilcut.geometry_demo

# 2) 按 materials/731/operations.jsonl 跑完整流程，导出 D1 标签
python3 -m coilcut.cli run \
  --db /tmp/coil731.db \
  --stock materials/731/stock.json \
  --operations materials/731/operations.jsonl \
  --delivery-dir /tmp/coil-dells

# 3) 用材料提供的读取方消费交付标签
python3 materials/731/delivery_reader.py /tmp/coil-dells/D1.json
# => D1 L2 ROOT 80 100 -1 20

# 4) 查询物理账 / 谱系 / 幂等回放
python3 -m coilcut.cli query --db /tmp/coil731.db --root ROOT
python3 -m coilcut.cli query --db /tmp/coil731.db --piece L2
python3 -m coilcut.cli query --db /tmp/coil731.db --cut C2

# 5) 失败模式与翻面后第二刀的完整演示
python3 -m coilcut.demo

# 6) 测试（含 20 根材 / 500 次切割的规模测试、并发争父件）
python3 -m unittest discover -s tests -v
```

## 关键语义

### 局部坐标与翻面

* 根材 `root_id` 上是半开区间 `[0, L)`。
* 每个活动子件有 `piece_id`、根来源 `[root_lo, root_hi)`、方向 `orientation ∈ {+1, -1}`
  与 `revision`。
* 局部 `u` 从**当前方向起点**量：
  * `+1`：局部 `0` 在 `root_lo`，`u ↦ root_lo + u`。
  * `-1`：局部 `0` 在 `root_hi`，`u ↦ root_hi - u`。
* 一次切割给局部 `u` 与正刀缝 `k`，刀缝占局部 `[u, u+k)`。局部左子件
  `[0, u)`、右子件 `[u+k, length)`，两个都必须是正长度。
* `flip` 只反转方向，**绝不改变 `root_lo/root_hi`**。

731 场景（`ROOT` 长 100，`P0` 方向 +1，刀缝 2）：

| 操作 | 父件 | 局部 u | 局部左子件（根区间，方向） | 局部右子件 | 刀缝根区间 |
|---|---|---|---|---|---|
| C1 | P0 [0,100) +1 | 30 | L1 [0,30) +1 | R1 [32,100) +1 | [30,32) |
| flip | R1 | — | R1 仍 [32,100)，方向变 -1，rev 2 | | |
| C2 | R1 [32,100) -1 | 20 | **L2 [80,100) -1**，长 20 | R2 [32,78) -1，长 46 | **[78,80)** |

所以真实交付 D1 是 **L2 = 根 [80,100)、方向 -1、长 20**，不是 `[32,52)`。
“翻面后从左端 20mm 再切”在 `-1` 方向下是从物理**右端**（`root_hi`）往左量。
最终账：余料 30 + 46、交付 20、刀缝 4，合计 100。

### 事务与并发

* 每次 `cut` / `flip` / `deliver` 都在单个 `BEGIN IMMEDIATE` 事务内完成。
* 切割先核父件仍 `active` 且 `expected_revision` 相符，再调用纯几何服务，
  然后原子地：把父件置为 `retired`、插入两个 revision=1 的新子件、登记刀缝。
  任何一步失败整体回滚，**不消耗父件、不分配半个子件**。
* `cuts.parent_piece_id` 有 `UNIQUE` 约束，配合父件状态检查，保证一个父件
  不能被两个切割同时消耗；并发时只有一个事务能赢。
* `cut` / `flip` 强制要求 `expected_revision`，避免无前置条件的盲写。

### 幂等与冲突

* `cut_id`、`delivery_id` 是幂等键。同内容重复提交返回**原结果**（逐字节等价），
  内容不同则 `ConflictError`（`code=conflict`）。
* 只有成功事务才写幂等行；失败不占用成功的 id。失败后同一 id 仍可用于合法操作。
* 预览对象记录 `revision` 与 `orientation`；`commit_preview` 两者都复核，
  翻面后旧预览会以 `stale_revision` 拒绝，不会按旧方向偷偷提交。

### 交付

* `deliver` 要求 `piece_id` 的活动实际长度与 `required_length` 精确相等，
  然后将其从 `active` 转为 `delivered`。交付后禁止再切或翻转。
* 交付标签 `piece-delivery/v1` 保留根来源与方向，字段恰好为：
  `schema, delivery_id, piece_id, root_id, root_lo, root_hi, orientation, length`。
  材料提供的读取方可直接读取。

### 物理不变量

`root_report(root_id)` 汇总该根材上所有 `active` 子件、所有 `delivered` 子件
和**全部刀缝**，按根区间排序后检查：

* 每段都在 `[0, L)` 内；
* 段与段不重叠；
* 没有空隙，且总和恰等于 `L`。

退役父件保留在 `pieces` 表中用于谱系追溯，但**不计入**物理账，因此不会重复计入。
任何重叠/缺口/越界都会抛 `InvariantError`。

### 谱系

`lineage(piece_id)` 沿 `cuts` 反向走到根，逐刀给出：父件、父件切割时 revision
与方向、该父件在切割前的翻转历史、兄弟件、局部 `u`、刀缝根区间；并列出该件
自己的翻转历史、作为父件的所有切割、以及交付 id。可完整解释父件、两子件和刀缝。

## 设计取舍：物化根坐标 vs 局部分换链

本实现选择**物化每个子件的根坐标**（`pieces.root_lo/root_hi`），同时保存
`cuts` 中的局部 `u`、刀缝和父件切割时方向：

* **谱系读取成本低**：任意子件的物理来源是一次主键查询，无需沿翻转链重算。
* **翻转后解释直截了当**：翻面只改 `orientation`，根本来源不动，避免“左端”
  在局部/根轴之间来回换算出错。
* **历史可追溯**：`flips` 记录每次 `old→new revision / orientation`，`cuts`
  记录父件切割时方向，因此仍能重建当时的局部视角。
* **一致性**：几何服务是唯一计算根坐标的地方，物化值在写入事务内直接来自
  `cut_geometry` 的结果，公开根来源与几何输出必然一致。

代价是存储略多、切割时多写两列；相对于最多 20 根材 / 500 次切割的规模可以忽略。
若改为只存局部变换链，则每次读取谱系都要重放翻转，且更容易在方向反转时把
“左端”误解成根轴左端。

## 错误码

| code | 含义 |
|---|---|
| `invalid_kerf` | 刀缝非正 |
| `out_of_bounds` | 切割/刀缝越出父件或根材 |
| `zero_length_child` | 会产生零长子件 |
| `not_found` | 未知 piece/cut/delivery/root |
| `piece_consumed` | 件已退役或已交付，不能再切/翻/交 |
| `stale_revision` | `expected_revision` 不符（含翻面后的旧预览） |
| `stale_orientation` | `expected_orientation` 不符 |
| `duplicate_piece_id` | 子件 id 已存在 |
| `conflict` | 幂等键被不同内容复用 |
| `length_mismatch` | 实际长度 ≠ 所需长度 |
| `invariant_violation` | 根材物理账重叠/缺口/越界 |
| `already_initialized` | 台账已初始化，拒绝重复灌入 |
| `invalid_operation` | 未知操作或演示断言不符 |

## 目录

```
coilcut/
  geometry.py     纯几何：局部↔根轴映射、切割、翻面，无状态无库存
  storage.py      SQLite 台账：事务、幂等、谱系、物理账
  operations.py   JSONL 操作协议
  cli.py          命令行
  demo.py         731 场景 + 全部失败模式演示
  geometry_demo.py 纯几何演示
tests/
  test_coilcut.py 几何、台账、翻面、交付、并发、规模、重开
```
