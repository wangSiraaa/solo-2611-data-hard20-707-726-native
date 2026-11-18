# 兑换服务实现说明

本目录实现独立的**资格服务**与**兑换台账写入侧**，接入 `materials/709` 的合成
SQLite 历史。金额全部为整数分；模板有稳定 ID 与不可变版本。

```
coupon_service/
  qualification.py   纯资格/组合规则（不读余额、不自动选券）
  ledger.py          事务性兑换 / 撤销 / request_id 冻结与复读
  catalog.py         模板版本发布与当前指针
  models.py          请求校验与冻结内容（JSON 值语义）
  errors.py          拒绝分类：资格 / 组合 / 所有权 / 余额
  db.py              SQLite 连接与附加表
  operations.py      operations.jsonl 回放器
  snapshot.py        只读快照（兼容 ledger_reader.py）
  cli.py             命令行入口
run_history.py       复制材料库 → 推进 operations → 调用只读客户
tests/               41 个测试
```

## 快速开始

```bash
# 从材料库复制到工作目录并推进历史（不修改材料原件）
python3 run_history.py
# 只读客户查看旧订单/剩余次数/撤销
python3 ledger_reader.py data/history.sqlite
# 测试
python3 -m unittest discover -s tests
```

## 历史库的接入方式

- `materials/709/history.sqlite` 只读；`run_history.py` 复制到 `data/history.sqlite`。
- **不重放历史成功兑换、不重设余额**。`O_OLD` 的 `redemptions` 与
  `redemption_uses` 原样保留，`coupon_instances.remaining` 就是扣后事实
  （`I_FOOD=1, I_SHIP=1, I_ALT=0, I_OTHER=1`）。导入时没有任何“初始化/回填”扣次。
- 原表与列含义不变。仅新增附件表：
  - `cancellation_results(cancel_id PK, order_id UNIQUE, user_id, request_json, result_json)`
    —— 冻结撤销结果（实际返还明细）。
  - `request_results` 为材料原有表，继续保存**首次请求**内容与结果（含拒绝）。
- `ledger_reader.py` 原样可用（测试 `test_legacy_reader_still_reads_original_columns`
  与 `test_material_history_is_not_reinitialised` 覆盖）。

### operations.jsonl 的真实推进结果

| # | 操作 | 结果 |
|---|------|------|
| 0 | revise T_FOOD → v2（门槛 1500） | `coupon_templates` 新增 v2，`current_templates` 指向 v2 |
| 1 | redeem `R_NEW` (O_NEW, 1200) | 资格拒绝 `QUALIFICATION/below_minimum`，冻结，两余额不变 |
| 2 | cancel `R_CANCEL` (C_OLD, O_OLD) | 按 `redemption_uses` 各返 1 → `I_FOOD=2, I_SHIP=2` |
| 3 | 重复 `R_CANCEL` | 复读冻结结果，不再返还 |
| 4 | redeem `R_MUTUAL` (I_FOOD+I_ALT) | 组合拒绝 `COMBINATION/mutually_exclusive_group`，不扣次 |

模板修订**改变新兑换资格**（`test_revision_changes_new_eligibility_not_display`、
`test_preflight_uses_current_version_after_revision`），而不只是改展示：
资格服务每次从 `current_templates` 解析具体版本。

## 资格服务（只消费明确模板版本）

`evaluate(QualificationRequest)` 的输入是完全显式的：

- 订单行 `(sku, qty, unit_price)`，`qty ≥ 1`、`unit_price ≥ 0`；
- 每个券实例的 `(instance_id, owner_user_id, template_id, template_version)`；
- 每个 `(template_id, version)` 的具体 `TemplateSpec`。

它**不读取余额**、不查询“最新版本”、不自动选券。规则：

1. 适用原价 = 所有 SKU 命中模板集合的订单行 `qty*unit_price` 之和；重复 SKU 行逐行相加。
2. 门槛以**适用原价**比较，且在该券自身的其他优惠之前（不先扣别的券）。
3. 同互斥组最多一张；不同组可叠加。
4. 总减免 ≤ 订单全部原价，否则 `QUALIFICATION/discount_exceeds_gross`。
5. 同一券实例在申请列表重复 → `COMBINATION/duplicate_coupon_instance`。
6. 模板不适用 → `template_not_applicable`；版本未提供 → `unknown_template_version`。

拒绝分为四类（`errors.family`）：

| family | 触发 |
|--------|------|
| `QUALIFICATION` | SKU 不适用、门槛不足、总减免超额、未知模板版本 |
| `COMBINATION` | 同互斥组多张、券实例重复 |
| `OWNERSHIP` | 实例属于其他用户；撤销时订单属于其他用户 |
| `BALANCE` | 资格合法但某券次数为 0 |

## 台账写入侧（兑换）

`ledger.redeem(con, request_id, request)`：

1. `BEGIN IMMEDIATE` 获取写锁。
2. 若 `request_results` 已有该 `request_id`：内容按 JSON 值相等则**复读冻结结果**
   （含拒绝），否则拒绝 `REQUEST_CONFLICT`；跨类型（redeem/cancel 共用 ID）同样拒绝。
3. 若 `order_id` 已成功兑换：内容相同 → 复读原成功结果 + 当前状态（`ACTIVE`/
   `CANCELLED`），**即使已撤销也不重新扣次**；内容不同 → `REQUEST_CONFLICT`。
   新 `request_id` 只能尝试**未成功**的订单。
4. 在**同一写事务内**固定目录版本：读取 `current_templates` 得到 `(template_id,
   version, spec)`——这就是“事务内界定目录版本”。随后按上述规则计算资格/组合，
   并校验所有权与 `remaining ≥ 1`。`BEGIN IMMEDIATE` 阻止其他连接在同一事务
   内提交模板修订或扣次，因此资格判定、余额校验与扣减看到同一快照。
5. 任一券不可用 → 整体拒绝，**其他券也不扣**。成功时对每个申请券：
   `UPDATE ... SET remaining=remaining-1 WHERE remaining>=1`（行数必须为 1），
   写 `redemptions`、`redemption_uses`（含 `template_version`、`discount`），
   冻结 `request_results`，最后 `COMMIT`。

两连接争最后一次：`test_two_connections_race_for_last_use` 证明恰好一单成功，
另一单得到 `BALANCE`，余额归 0。

## request_id 冻结语义

- 首次请求内容与结果冻结，**包括拒绝**；重复同 ID 同内容复读原结果。
- 内容按 **JSON 值**比较：对象键序无关（`json.dumps(sort_keys=True)`），
  订单行与券列表**顺序保留**（`test_list_order_is_significant_for_content_equality`）。
- 拒绝结果持久化，但绝不写 `redemptions`/`redemption_uses`、不扣次。
- 成功兑换保存当时模板依据与优惠：`redemptions.qualification_json` 含每模板
  `(version, spec)` 与应付；`redemption_uses` 记录 `template_version`、`discount`。
  之后模板修订不改原事实（`test_later_revision_does_not_change_old_fact`）。

## 撤销

`ledger.cancel(con, request_id, request)`：

- 必须关联原成功订单，校验同一用户（否则 `OWNERSHIP`）。
- 每笔订单仅一次：`cancellations(order_id UNIQUE)` + `redemptions.status='CANCELLED'`
  双重保护；重复 `request_id` 复读；新的 `cancel_id` 指向已撤销订单 →
  `CANCEL_CONFLICT`，不返还；同一 `cancel_id` 用新 `request_id` 重试 → 复读原撤销事实。
- 返还严格依据 `redemption_uses.used_count`，与模板后来停用/门槛变化无关
  （`test_cancel_refund_uses_original_uses_after_template_revision`）。
- 返还更新、`cancellations`、`redemption.status`、冻结结果在同一事务提交，原子。

## 只读查询接口

- `ledger.preflight_redeem(con, user_id, lines, coupon_ids)`：只读评估，可区分四类拒绝。
- `ledger.current_balances`、`ledger.order_status`、`snapshot.snapshot`。
- CLI：`python -m coupon_service.cli qualify|snapshot|redeem|cancel|run`。

## 事务内目录版本与扣次（可选提交说明）

- **目录版本固定点**：在 `BEGIN IMMEDIATE` 之后、任何余额读取/扣减之前读取
  `current_templates`，得到具体 `version` 与其 `spec_json`。该版本和优惠随成功
  结果一起写入 `redemption_uses` / `qualification_json`，成为不可变事实。
- **扣次**：同事务内先条件 `UPDATE`（`remaining>=1`）再写事实；任何一步失败则
  `ROLLBACK`，不会出现“扣了但没记账”或“部分券已扣”。
- **并发**：SQLite 单写者锁（`busy_timeout` 15s）使资格判定与扣次串行化；
  两连接争最后一张券至多一单成功。
- 撤销同样在单事务内完成返还 + 事实写入；已撤销订单无法再次返还。

## 范围外（按要求未实现）

部分退款、比例折扣、自动选券、进程强杀协议均不实现。规模上限
20 模板 / 200 实例 / 500 订单。

## 测试覆盖

```
python3 -m unittest discover -s tests
```

- 资格：重复 SKU 行累加、门槛用适用原价、门槛先于其他优惠、同组互斥/跨组叠加、
  总减免上限、实例重复、所有权、未知版本、空券不自动选。
- 台账：历史不回填、撤销返还一次、重复撤销、异 cancel_id、跨修订返还、
  历史请求复读不重扣、键序无关、列表顺序敏感、订单内容永久绑定（含撤销后）、
  新 request_id 重试失败订单、余额失败不扣其他券、组合失败不扣、两连接竞争、重开。
- 目录：成功时固定版本、后续修订不改旧事实、版本不可变、版本仅前进、
  写事务目录边界、修订影响 preflight。
- 操作回放：5 条 operations 的期望结果、可重放、只读客户兼容、材料原件不变。
