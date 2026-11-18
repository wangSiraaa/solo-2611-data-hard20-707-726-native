# 有身份的历史观测
observation_feed.py读取实际CSV，输出counter-observations/v1；运行`python3 observation_feed.py observations.csv`。文件故意按采集文件顺序记录，服务不能假定这是时间排序，也不能把乱序等同非法。revision.json插入历史O1，operations.jsonl后续更正和删除同一身份，最终每设备时间必须唯一。

手算D1：R0的0→4段，模10差6，最大8，唯一增量6，两个班各分3。插入t=2读0后，两段唯一增量2和4，R1为2、4。再把t=4读数改3，R2为2、3。删t=2后0→4唯一增量5，R3两班各5/2。R0历史仍为3、3。D2在0→4读8到0，最大12，可为2或12，须缺口，不得记作2或0。bad新增同设备t=4应整批拒绝。
