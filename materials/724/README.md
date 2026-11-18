只提供真实原始条目、错误缓存、历史已交付文件和只读客户端，没有旧评分后端或目标实现。
运行起点：python3 legacy_reader.py legacy_scores.json --catalog legacy_catalog.json。
entries是各量表权威定义，direction_cache_by_question不能表达同题跨量表方向。旧HIST_SCORE_001特意保留当时结果及口径，不得覆盖或据它断言当前评分正确。

新增scoring-product/2合同：format、report_id、scoring_contract（per-scale-entry）、scores；每条score保存respondent_id/answer_revision/scale_id/scale_version/eligible/missing_weight/answered_weight/weighted_sum/min_total/max_total/mean/entries。mean用既约分数字符串a/b或undefined。entries逐题保存question_id/min/max/weight/reverse/answer/contribution；缺失答案为MISSING，contribution为null，并给该条目的可能贡献上下界。无资格条目的正式总分/均分为null，理由明确。可增字段。客户端升级后能读旧历史口径和新产品，旧值原样读取，不以当前目录暗改旧分。

手算U1修订1：A已答Q1=5×2=10，缺Q2权重1允许，总分10至12、均分5；B对Q1反向为1，得1、界限1至1、均分1。旧文件B记5是已公开错误，不是新实现的期望。
U2在C全缺但K=3允许：和0、已答权重0、总分2至12、均分undefined；在A缺失权重3>K1，不具资格。
执行operations后U1修订2在A版本2：Q1反向为2乘权重2，和4、已答权重2、缺题范围0至2，总分4至6、均分2；在B版本1和2、界限2至2、均分2。旧A版本1及历史文件仍可读。
