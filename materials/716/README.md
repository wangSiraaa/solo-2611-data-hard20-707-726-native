# 716 价格输入与账单读取合同

catalog.json、orders.json、operations.json是新制作输入，不是已有计价系统。operations按顺序说明提交、单行修订及回读；修订只替换指明字段，其余字段继承当前价目行。目录版本2先将b-high单价改200，版本3再将a-base改120。

invoice_client.py是可运行的独立只读客户端，只核对已给单价/数量的账单结构并输出CSV，既不读价目目录也不选择报价。目标实现须输出下列公开格式供它实际消费：

```json
{"format":"order-receipt/v1","order_id":"o-1","grade":"gold","catalog_version":2,"total_amount":900,"lines":[{"line_id":"left","sku":"A","quantity":2,"total_sku_quantity":5,"unit_price":100,"amount":200,"price_ids":["a-base"]},{"line_id":"right","sku":"A","quantity":3,"total_sku_quantity":5,"unit_price":100,"amount":300,"price_ids":["a-base"]},{"line_id":"side","sku":"B","quantity":2,"total_sku_quantity":2,"unit_price":200,"amount":400,"price_ids":["b-high","b-main"]}]}
```

同价最高层所有price_ids排序保存。行可重排但line_id保持。客户端命令：`python3 invoice_client.py RECEIPT.json`。
reader-example-receipt.json只是手写的单行客户端格式样例，不是给定订单的求解实现。

o-1第一次因B最高层200/220歧义整体失败；版本2成功总900；版本3后读取同一成功订单仍为900。A须以2+3=5查价，不能分别按2和3件取价。只要求这些指定数量点及正常边界，不扫描全部数量域。
