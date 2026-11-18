# 嵌套资产身份与保管责任交接

两个彼此独立、通过明确接口联动的 SQLite 存储，只处理**合成身份**：不核 SKU
库存、不核价格、不做装箱相容、不做货量预留或仓库路线。

| 存储 | 文件 | 知道什么 | 明确不知道什么 |
| --- | --- | --- | --- |
| 资产目录 `AssetCatalog` | `catalog.sqlite`：`assets` / `edges` / `meta` | 稳定 `asset_id`、`CONTAINER`/`ITEM` 类型、至多一个父的物理森林、目录修订号 | 任何保管人 |
| 交接仓 `HandoffWarehouse` | `warehouse.sqlite`：`custody` / `handoffs` / `handoff_members` / `meta` | 当前保管人、准备单、冻结的历史成员、责任版本号 | 父子边、森林结构 |

两侧的实质联动只有一条方向：交接仓在 prepare/提交校验时调用目录的
`expand_roots()`、`get_asset_type()` 与 `revision`。目录里没有保管人列，交接仓
里没有父子列，任何一方都无法单方面推出另一方的事实。

## 运行

```bash
# 端到端回放 materials/755 的小森林、两次交接与中间挪动；
# 同时用材料自带的只读 handoff_reader.py 子进程校验产出工件
python3 -m asset_handoff.replay --out-dir runtime

# 只读客户端直接读实际交接工件
python3 materials/755/handoff_reader.py runtime/artifacts/H1.json   # COUNT 4
python3 materials/755/handoff_reader.py runtime/artifacts/H2.json   # COUNT 3

# 测试（标准库 unittest，无第三方依赖）
python3 -m unittest discover -s tests -v
```

## 目录语义

- 每资产有稳定 `asset_id` 与类型；只有 `CONTAINER` 能作父，任一资产至多一个父。
- `revise_membership(attaches=..., detaches=..., moves=...)` 是**批操作**，先在
  内存里合成最终森林再整体判合法：父必须存在且是容器、禁止重复身份/重复边、
  禁止环（含自环）、根到节点边数不超过深度 8。批内任一资产被改两次直接拒绝，
  避免歧义批次藏重复身份。
- 任何变更批都让 `membership_revision` 加 1；初态引导载入不递增（材料里
  `assets.json` 自带修订号 1，第一次真实挪动后是 2，与 operations 对齐）。
- 查询：`parent_chain()`（自身到根）、`subtree()`（整棵子树身份）、
  `expand_roots(roots)`（扫描根集合展开）。
- 展开规则：包含根容器本身；按 **asset_id** 去重（先到先占的前序 DFS，兄弟按
  id 排序，输出确定）；扫到祖先又扫到后代时后代只计一次；不同 id 的资产即使
  调用方视为"同名"也不去重。

## 保管责任与交接语义

- 物理嵌套不转移责任：`revise_membership` 只碰目录库，交接仓的 `custody` 一行
  不动（回放里 I1 移到 C3 后保管人仍是 B）。
- `prepare(handoff_id, roots, from, to)`：
  1. 调目录展开扫描根，逐资产查当前保管人；
  2. **任一成员**当前保管人 ≠ `from` 即整单拒绝（`CustodianMismatchError` 带
     全部不符项），外箱属于交出方不推定箱内资产也属于他；缺责任记录同样是
     不符而不是继承；
  3. 通过后立刻把完整成员（id + 当时类型 + 当时保管人）、扫描根、目录修订号、
     责任版本号物化成 `PREPARED` 行。
- `commit(handoff_id)`：先做三重失效/一致性检查，再在**一个事务**里把冻结成员
  集合的当前责任全部改到接收方并把单据置为 `COMMITTED`。绝不是只改根容器字段。
- 失效条件（`PreparedStaleError`，整单不提交、责任不变）：
  - 目录修订号变化（加资产或任何森林改动，即使发生在扫描集合之外）；
  - 责任版本号变化（任何保管人写入，即使与本单无关——保守但绝不会漏失效）；
  - 兜底再逐资产复核冻结成员仍由 `from` 持有。
- 历史冻结：提交后的成员清单与目录修订号固定为 prepare 时的展开结果；之后挪动
  资产只改当前森林，不重算历史成员。新交接必须重新 prepare，消费当前目录。
- 目录在无任何交接时可独立查询；交接仓不向目录硬编码保管人。

## 关键设计权衡（题目明确要求自组织的部分）

1. **冻结成员清单 vs 仅引用不可变目录快照**：选择在 prepare 时**物化完整成员
   行**（`handoff_members` 含 asset_id/类型/位置/当时保管人），而不是只存一个
   快照引用。理由：重开后历史正确性不依赖任何快照 GC、也不依赖当前目录；代价是
   成员冗余存储，在 ≤200 资产规模下可忽略。工件里再带一份冻结类型，使只读
   客户端无需任何数据库即可核历史。
2. **重复扫描去重**：展开期用 id 集合 + 前序 DFS 一次完成，时间 O(成员数)，
   去重依据是 `asset_id` 而非名字；确定性顺序（兄弟排序）让工件可逐字节比较。
3. **当前责任 / 历史成员 / 批修改的组织**：当前责任是每资产一行的可变表；历史
   成员是只追加的冻结表；批修改的"最终森林"在内存合成后单次事务落库。提交同样
   是单事务，失败不留半截责任。
4. **失效检测成本**：用两个单调版本号（目录修订号 + 责任版本号）做 O(1) 的
   prepare/commit 校验，外加逐成员责任复核兜底；全局责任版本号对无关改动也会
   使准备失效，这是有意的保守选择（漏失效比误失效危险）。
5. **环/深度校验**：迭代走父链 + 根相对深度 memo，无递归（不受 Python 递归
   限制），在 200 资产 / 深度 8 下开销可忽略；环检测对边插入顺序不敏感（已用
   全排列模糊测试验证）。

## 代码布局

```
asset_handoff/
  errors.py     # 领域异常（不符、失效、环、深度、重复身份等）
  catalog.py    # 独立身份目录：assets/edges/meta，森林校验与展开
  warehouse.py  # 独立交接仓：当前责任、准备单、冻结成员、asset-handoff/1 工件
  replay.py     # 消费 materials/755 的 JSONL，产出 runtime/ 下的库与工件
tests/          # 36 个 unittest：目录、交接仓、端到端回放、只读客户端、规模
```
