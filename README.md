# 累计用量边际计费与已发账单追补

合成合同、合成用量与合成账单的独立实现。不解释、不推断真实资费或税务。

## 组成

| 模块 | 作用 |
| --- | --- |
| `cumulative_billing/contract.py` | 边际档合同校验与 `F(x)`；半开档 `[lower, upper)`，从 0 起连续、不重叠、全覆盖，末档无上界。 |
| `cumulative_billing/pricing.py` | 独立累计计价服务：按账号/年/期间累计，给出每期 `累计起止 / 边际贡献 / 当前费用`。 |
| `cumulative_billing/repository.py` | 追补仓：导入已发单（原文不动）、记录用量修订、按当前费用与实际净额签发有符号差额。 |
| `cumulative_billing/documents.py` | `period-bill/1` 校验与序列化；原单 `BILL`、差额 `ADJUSTMENT`。 |
| `cumulative_billing/store.py` | SQLite 表结构。 |
| `cumulative_billing/cli.py` | `init / import-usage / revise-usage / import-bills / recalc / reassess / export / replay`。 |
| `tests/test_billing.py` | 30 项测试：边际 F、跨期传播、幂等、按净额更正、缺期、三档合同、客户端读取。 |
| `demo.sh` | 跑通 754 场景并让 `bill_reader.py` 读取导出。 |

## 计价规则

- `F(x)` 把前 `x` 个单位逐段落到各自边际档求和；某期费用是
  `F(本期累计末) - F(本期累计初)`，既不是“本期数量单挑一档”，也不是“全部累计量套末档价”。
- 每账号按合成年、整数期间 1..12 累计；输入是非负整数已确认用量，不处理原始计量器或回绕。
- **空缺前期不是 0。** 只有存在用量记录（含显式 `0`）才算已确认。
  在最后一个已确认期间之前出现的缺期会中断累计链，`complete_through` 停在缺期前一格，
  之后的正式费用不发布；纯尾部尚未上报的期间不算缺期。
- 计价服务独立读取合同和用量修订，不依赖账单仓。

## 追补语义

- 导入保留旧单 `document_id`、`amount_cents`、原始 JSON 文本（导出行逐字节不变），绝不从零重开历史。
- 重算 `1..through_period` 时，逐期比较 `当前费用` 与 `该期已发净额`（原单 + 该期全部差额）。
  只有非零差额才签发新的 `ADJUSTMENT`，所以：
  - 未变化期间不产生凭空差额；
  - 同一更正重复执行不重复签发（净额已等于目标）；
  - 后续更正相对 **净额** 计算，而不是再次相对最初原单。
- 前期更正通过累计位置自然传播到其后所有受影响期间（`F(end) - F(start)` 改变）。
- 差额文档带符号（`amount_cents` 可负），`previous_statement_id` 指向被调整原单；
  调整身份是 `(account, year, period, sequence)`，保证可追溯与不重复。
- 差额保留 `account_id / year / period / contract_revision / usage_revisions`，
  另加 `sequence / target_amount_cents / previous_net_cents / recalc_run_id` 来源字段。

## 754 场景结果

初始累计 80/130/150，费用 160/70/20，净额 250。
首期改为 60 后累计 60/110/130，费用 120/90/20：

| 文档 | 期间 | 金额 | 指向 |
| --- | --- | --- | --- |
| B1 | 1 | 160 | — |
| ADJ1-P1-1 | 1 | −40 | B1 |
| B2 | 2 | 70 | — |
| ADJ1-P2-1 | 2 | +20 | B2 |
| B3 | 3 | 20 | — |

后月数量 50 没变却因跨回较贵累计档多 20；重复执行不重复签发；客户端净额 `NET 230`。

## 用法

```bash
python3 -m unittest discover -s tests -v     # 30 项测试
./demo.sh                                     # 754 场景 + 客户端读取
```

单步：

```bash
python3 -m cumulative_billing.cli init --db out/billing.db --contract materials/754/contract.json
python3 -m cumulative_billing.cli import-usage --db out/billing.db --usage materials/754/usage.jsonl
python3 -m cumulative_billing.cli import-bills --db out/billing.db --bills materials/754/issued_bills.jsonl
python3 -m cumulative_billing.cli revise-usage --db out/billing.db \
  --account AC1 --year 2025 --period 1 --revision 2 --quantity 60
python3 -m cumulative_billing.cli recalc --db out/billing.db \
  --account AC1 --year 2025 --through 3 --save-as ADJ1
python3 -m cumulative_billing.cli export --db out/billing.db --out out/bills.jsonl --account AC1 --year 2025
python3 materials/754/bill_reader.py out/bills.jsonl
```

`recalc` 在累计链不完整时拒绝发布正式费用（`IncompleteChainError`），不做部分推定。
