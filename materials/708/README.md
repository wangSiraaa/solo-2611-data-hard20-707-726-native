# 转换输入与报表客户
`directory.json`和`measurements.json`是输入；`revision.json`是一批按最终边集生效的删替，`invalid_revision.json`用于检查拒绝后仍为r2。`operations.jsonl`给出顺序。偏移修订是合成校准，不代表真实温标改变。

`report_reader.py`是报表格式读取起点，不实现求解。运行`python3 report_reader.py reader-example.json`可确认读取合同；example仅格式示例，不是目标结果。正式服务生成同schema的评价列表，保留这些字段，可增加规范快照、路径等字段。

手算：r1的50 C绝对值为122 F，命中[121,123]；r2为120 F，不命中。10 C温差两版均18 F。替换E_CF时临时旧E_KF会冲突，但完整r2批次最终一致。r2后仅将E_CF改为31的bad批次与E_KF2矛盾，应整批拒绝。
