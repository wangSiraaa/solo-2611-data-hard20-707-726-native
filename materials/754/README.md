材料中issued_bills.jsonl是已发合成账单事实，不是目标计价实现。保留ID和旧金额，不能从零重新开旧单。
python3 bill_reader.py issued_bills.jsonl
只读客户端当前打印B1/B2/B3的160/70/20和NET250；新差额也用period-bill/1，document_type=ADJUSTMENT、amount_cents可负、previous_statement_id指被调整原账单。保留account_id/year/period/contract_revision/usage_revisions；可加新的累计/来源字段。

边际F(x)：x<=100时2x，超过时200+(x-100)。初始累计80/130/150，各期160/70/20。
首期改60后累计60/110/130，费用120/90/20；因此B1差额-40，B2差额+20，B3无差额，净调整-20，原单加调整总净额230。后月用量50没变却涨20，正是本题跨期核心，不是716的本期规则择价。
重复同一更正不可重复发-40/+20。目标后端、跨期传播与差额产品均由候选实现，客户端不执行这些逻辑。
