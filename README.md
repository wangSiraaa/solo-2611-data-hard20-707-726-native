# 独立展馆分区 / 门段目录与路线核查事实库

纯 Python 3.11 标准库实现（SQLite 持久化、`fractions.Fraction` 精确有理几何），
只处理 **合成平面** 的目录核验与给定折线核查，不涉及实体门禁、机器人尺寸、
碰撞、通行权限或路径优化，也不会替调用方寻找路线。

## 运行

```bash
# 对 materials/733 执行 operations.jsonl 的完整事实核查
python3 -m gate_audit.cli --materials materials/733 \
    --db out/gate_audit.sqlite --outdir out/reports

# 测试（22 个，含 30 室 / 80 门 / 200 路线 / 每路线 100 段规模测试）
python3 -m unittest discover -s tests -v
```

## 模块组织

| 文件 | 职责 |
| --- | --- |
| `gate_audit/geometry.py` | 有理点/矩形、正面积与内部重叠判定、正长度共边、线段 t 区间裁剪、开放区间并 |
| `gate_audit/catalog.py` | 平面版本目录：房间/门独立核验、带所有权的边段索引、门修订、SQLite 持久化、独立共边门查询 |
| `gate_audit/verifier.py` | 折线核查：事件点切分、严格内部驻段、门开集穿越裁决、结点合并、首个非法位置 |
| `gate_audit/reports.py` | 批次报告冻结、同号复读/异文拒绝、门证据快照、通行凭据 |
| `gate_audit/cli.py` | 按 `operations.jsonl` 顺序驱动，报告落盘到 `out/reports/` |

## 几何与门的精确语义

1. **房间**：轴对齐闭矩形，必须正面积；两两 *开内部* 不相交，边/角可重合。
   内部重叠在目录载入时直接拒绝（`CatalogError`）。
2. **共边**：只有投影交叠为正长度才算共边（`SharedEdge.length > 0`）；
   四角相遇只有一个公共点，`shared_edge()` 返回 `None`，该房间对不能设门。
3. **门是开放线段 `(lo, hi)`**：门框端点 `lo/hi` 不可通行。
   门必须整体落在声明两室的真实共边闭区间内，且方向（竖边沿 y、横边沿 x）
   与共边一致，否则目录拒绝该平面版本。
4. **重叠门按开集并解释**：两扇门真正交叠时，其开集并可通行；
   仅端点相接的两扇门 **不** 合并——接触点不属于任何开集，不能自动填成可通行
   （见 `geometry.merge_open_union` 与测试 `test_open_union_does_not_fill_touch_point`）。
   穿越点被多扇门同时覆盖时，报告列出全部 `all_gate_ids`，主门取最小 gate_id。

## 路线核查算法（不寻路）

对每条折线的每个线段 `P(t), t∈[0,1]`：

1. 与所有房间闭矩形做精确一维裁剪，得到线段落在各房间内的 t 闭区间；
2. 取所有区间端点为事件点，把 `[0,1]` 切成开区间；
3. 每个开区间的中点必须位于 **恰好一个** 房间的严格内部：
   * 中点在墙上（被某闭矩形包含但不在内部）→ `WALK_ALONG_WALL`（沿墙走一段）；
   * 中点在所有房间之外 → `OUTSIDE_PLAN`（越过未定义空间）；
4. 事件点逐一裁决：左右为不同房间时，核对两室 **正长度共边** 与该坐标上的
   **开放门段**（不是只查邻接图）；角点无共边 → `POINT_CONTACT_NO_SHARED_EDGE`，
   门框点 → `GATE_FRAME_ENDPOINT`，共边上无门 → `NO_GATE_ON_EDGE`；
5. 折线 **结点** 先按其自身几何位置分类：结点在门上时把前后两段合并成一次
   穿越（`at_node=true`，只计一次）；结点在墙上但两侧同房 → 擦墙返回；
   对角转折无共边门 → 拒绝，不能借转折跳进对角房；
6. 零长段（含在墙上等待）→ `ZERO_LENGTH_SEGMENT`；起终点不在严格内部 →
   `ENDPOINT_NOT_INTERIOR`，若该端点恰在门边界上还会同时给出门框/无门语义。

输出按路线顺序给出：房间驻段 `room_stays`（开区间 + 段号 + t）、
门 ID/坐标/两侧房间的 `crossings`、`room_chain`，以及按 (段号, t) 排序的
**首个** 不合法位置与原因。非法路线 `credential_id` 恒为 `null`，
凭据表中也不会出现该路线。

## 三项组织取舍

* **共边重合 / 带所有权的边段索引**：目录载入时一次性预编译
  `(room_a, room_b) → SharedEdge` 与 `edge_gate_index`（门带其所属房间对），
  核查穿越点时按房间对直接取共边与门，O(门数) 而非扫全平面；同时路线核查
  保留“对路径逐房求交”的朴素实现作为唯一语义来源，索引只做加速与独立查询，
  不参与合法性判定，避免两套几何结论漂移。
* **门开集**：全部坐标存为规范分数字符串，判定统一用严格不等式 `lo < v < hi`；
  开集并在独立查询中物化、在穿越裁决中即时计算，二者共用同一比较规则，
  门框端点在任何入口都不可通行。
* **版本失效**：门修订 (`replace_gate`) 生成全新 revision 行，旧版本的房间、
  门、共边、索引完整保留且不可变；每份报告冻结 `plan_revision`、
  `plan_content_hash` 与 `gate_evidence` 门证据快照。因此门修订后新报告
  结论改变（r2 的 VIA_B 撞门框被拒），而 `read_report R1` 永远返回 r1 的
  原门证据（AB 仍是 (1,3)、路线仍合法）。同一 `report_id` 请求哈希一致则
  原样复读（`replayed=true`，不重复签发凭据），异文直接拒绝，禁止覆盖。

## 材料核查结果（materials/733）

| 版本 | 路线 | 结论 | 首个非法点 |
| --- | --- | --- | --- |
| r1 | VIA_B | 合法，凭据 A→B→D（门 AB、BD） | — |
| r1 | CORNER | 拒绝 | (4,4) `POINT_CONTACT_NO_SHARED_EDGE` |
| r2（AB 改 (2,4)） | VIA_B | 拒绝，不发凭据 | (4,2) `GATE_FRAME_ENDPOINT` |
| r2 | CORNER | 拒绝 | (4,4) `POINT_CONTACT_NO_SHARED_EDGE` |
| 复读 | R1 | 返回 r1 冻结证据，结论不变 | — |
