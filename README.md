# 相容目录 + 独立装箱/发运库（虚构，非真实危险品规则）

Python 3.11 + 标准库（`sqlite3`），无网络、无密码学、无最优装箱。

## 目录结构

```
boxship/
  catalog.py       无向不相容类别对；多重集合冲突判定（不去重）
  catalog_cli.py   目录独立入口：只做规则检查
  core.py          SQLite 持久库：导入/装箱/封箱/拆封/导出/接收
  store_cli.py     装箱库入口（init/summary/catalog/create-box/move/seal/
                   unseal/apply-revision/export/export-bundle/receive/replay/
                   diagnose/items）
  receiver_cli.py  独立发运接收入口：只读 shipment-input/v1，与库核对
  ops_runner.py    按 operations.jsonl 顺序驱动“客户端→接收服务”，并演示扩展场景
tests/test_boxship.py
run/                演示生成物（数据库、客户端生成的 input JSON）
```

## 快速运行

```bash
python3 -m unittest discover -s tests -v            # 36 个测试
python3 -m boxship.ops_runner --materials materials/711 --workdir run
```

`ops_runner` 用子进程分别调用：

1. `materials/711/handoff_client.py` —— 只读外部文件，生成 `shipment-input/v1`；
2. `python -m boxship.receiver_cli` —— 只认 `shipment-input/v1`，与持久库核对。

两个入口之间无共享内存，接收方不导入客户端代码。`materials/711/handoff_client.py`
未做任何修改。

## 规则模型（catalog.py）

- 配对无向并存为集合；`(A,B)` 覆盖 B/A。
- 允许 `(A,A)`：同箱至少 **两件** A 才冲突（按实际件数，不去重）。
- 未声明的类别对相容。
- `Catalog.check(contents)` 输入可迭代的 `(item_id, category)`，返回
  实际冲突列表；每个冲突带规范化类别对和各类别**贡献件数**。
- 规则完全来自输入文档，不内置任何法规知识。

```bash
python3 -m boxship.catalog_cli --catalog materials/711/initial_store.json --items I2:A I4:A
python3 -m boxship.catalog_cli --catalog materials/711/revision.json      --items I2:A I4:A
```

## 持久库模型（core.py）

表：

| 表 | 作用 |
| --- | --- |
| `catalogs` | 每个修订号一份规则；同号不同规则被拒绝，保证“当时规则”可复核 |
| `items` | 稳定 `item_id`、类别、正整数重量 |
| `boxes` | 容量、状态 `OPEN/SEALED/SHIPPED`、当前有效 `current_seal_id` |
| `item_location` | 每件货物一行（`item_id` 为主键），同一时刻只在一个箱 |
| `sealed_boxes` | **只追加**的可信封箱记录：完整内容快照 + `live` 标志 |
| `shipments` / `shipment_boxes` | 发运身份、规范内容、结果、箱–封箱归属 |
| `events` | 审计轨迹 |

### 装箱/封箱

- 移动在一个 `BEGIN IMMEDIATE` 事务内先删位置再插位置；以下任一不合法则整体回滚：
  未知货物/箱、目标非 `OPEN`、源非 `OPEN`（已封/已发运不可绕过拆封搬运）、
  目标超容量。
- 开放箱允许暂存规则冲突；**封箱时**才用当前规则检查实际多重集合。
- 封箱失败保持 `OPEN`，内容不变。
- 封箱成功写入不可变 `seal_id`，并在 `sealed_boxes` 保存：封箱时规则修订、
  容量、按 `item_id` 排序的完整货物身份/类别/重量、总重、规则快照引用。
- 新 `seal_id` 由全局计数器生成，并额外查重历史，绝不复用。
- 拆封把旧记录 `live=0`、箱置 `OPEN`；再封使用**当前**规则并生成新 `seal_id`。
- `SHIPPED` 箱不能拆封、不能作为移动源/目标。

### 目录修订

`apply_revision` 只切换“当前修订”，供**下一次封箱**使用。已写下的封箱记录保存了
当时的 `rule_revision`，发运复核时读取该修订，不用当前规则追溯。

## 发运接收（receiver_cli / receive_shipment_input）

输入只接受 `shipment-input/v1`。每个 `manifest` 先做结构校验与规范化：

- 字段、类型、正整数重量；`contents` 内 `item_id` 唯一并按 `item_id` 排序；
- `total_weight` 必须等于内容重量和且不超容量（**仅证明工件自洽**，不足以可信）。

随后逐箱与持久库核对：

1. 箱子存在且状态为 `SEALED`；
2. 工件 `seal_id` == 箱子当前 `current_seal_id`（拒绝旧的失效身份）；
3. 存在 `live=1` 的可信记录，且工件与记录逐字段一致（拒绝改内容）；
4. 当前实际内容/总重与工件一致（防止库内漂移）；
5. 用**记录里的** `rule_revision` 复核实际内容。

整个申请在**同一事务**中：

- 若 `shipment_id` 已存在：规范内容相同 → 返回原成功结果（`replayed=true`）；
  不同 → 拒绝。规范内容与文件路径无关，`manifests` 按 `box_id` 排序，
  JSON 键序无关。
- 否则先逐箱核验，再统一写 `shipments`、把全部箱置 `SHIPPED`、写归属；
  任一箱失败则全部回滚，`shipment_id` 与箱子均不被占用。
- 同一申请中同一 `box_id` 出现两次 → 拒绝。
- 已发运箱不能再进入新发运（状态与唯一索引双重约束）。

**逐箱核验，不做整单类别并集**：`BOX_A(A)` 与 `BOX_B(B)` 分箱发运合法，尽管
整单类别并集 `{A,B}` 在 r1 下会冲突。

## 可信记录取舍

采用**完整内容快照**（`sealed_boxes.record_json`），而不是摘要 + 明细引用：

- 优点：单库即可独立复核，无跨表拼接歧义；记录不可变，历史清晰。
- 代价：存储冗余；内容多时体积大。
- 当前状态不放快照里：箱子“现在是否仍有效/已发运”由 `boxes.status` 与
  `boxes.current_seal_id` 表达，避免快照过期造成两处真相。
- 若改用摘要：可省空间，但复核/取证需要稳定的明细表；任何明细被改写都会
  使摘要失配，且需要额外机制保证明细不可变。本项目规模（≤100 件、20 箱）
  选择快照更直接。

## operations.jsonl 结果

`ops_runner` 严格按行执行并断言关键预期：

| 操作 | 结果 |
| --- | --- |
| `receive changed_shipment.json` | 自洽但内容与 `SEAL_A_1` 可信记录不符 → `manifest_mismatch`，箱子不被占用 |
| `seal BOX_M`（r1） | 两件 A → `conflict`，仍 `OPEN` |
| `apply_revision revision.json` | 切到 r2（移除 A-A） |
| `seal BOX_M` | 成功，`rule_revision=r2` |
| `receive shipment.json` ×2 | 首次 `replayed=false`；第二次 `replayed=true` |
| `unseal BOX_A` | 已发运 → `shipped_box` 拒绝 |

扩展场景（独立演示库）：容量超限移动原子失败、从已封箱搬出被拒、拆封后旧工件
失效且新 `seal_id` 不复用、r2 不追溯改写 r1 封箱、重开后再按 r3 封箱、
重复发运/异文/SHIP_ID 复用拒绝、同申请重复箱拒绝、100 件 20 箱规模。

## 限制

- 不加密码学签名、网络协议或非法装箱优化；可信性来自本地持久库而非工件本身。
- `shipment_id`/`seal_id` 为业务标识，没有防伪能力；伪造者若同时能改库，
  本设计不提供保护。
