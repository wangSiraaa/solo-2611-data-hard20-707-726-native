只提供输入、公开产品样例和只读客户端，不提供旧目录/事实后端。
运行读取起点：python3 consumer.py example_product.json。

catalog.json的边为[子,父]；facts.jsonl逐行一条已提交事实。operations.jsonl按顺序表示请求，B1要作为一个原子bundle；B_BAD使R→L形成环，连同金额999一起拒绝。queries.json给查询材料。修订水位字符串可自定，但必须稳定标识真实切面。

产品classification-summary/1结构见example_product.json：cut给catalog_revision/fact_revision；groups逐目标给count/amount/contributions，每条贡献有ticket_id、amount、path；union给按工单并集的count/amount及贡献。可增字段，不能改这些字段含义。客户端只打印其中部分字段，不代替正确性审核。

手算：初始A为T1/T2，件数2金额100；B为T1/T3，2和150；并集3和150。B1后A仅T2，1和0；B为T1/T3，2和170；并集3和170。不能出现A金额120或B金额150却声称已读到完整B1。B_BAD不得产生金额999。父子同时作为目标时仍对整组取工单并集。
