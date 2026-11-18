# 地址输入材料
这些合成文件是本题接入起点。`csv_client.py`只读CSV并保留原文本/来源行；目录解释、持久作业和修订由待实现服务负责。

运行：`python3 csv_client.py addresses.csv`。该输出的`address-input/v1`字段应可被服务接收；可增添字段，不改变现有字段含义。`operations.jsonl`说明输入和修订顺序，路径相对此目录。

手算：r1时P1有S1/S2两个候选，P2沿完整父链仅S3并派A；P3命中A/X两个站点。P5含CSV引号逗号，P6的分解重音需按NFC匹配S3。r2后P3派A，P4派A；P1仍有S1/S2两个地址候选，不能用停用或覆盖情况猜地址；新P7不能使用停用S2。已提交P2复读原事实。
