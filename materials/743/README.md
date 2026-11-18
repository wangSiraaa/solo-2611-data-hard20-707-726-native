# 743 多重集源与派生操作

R元组依次region/sku/quantity，S依次sku/supplier/price。相同元组multiplicity不是去重标记；delta合并后的最终源计数必须非负。input.json只给源、有限变更和操作，没有LEFT JOIN实现。

initial:east派生行计数3、未报价1、金额12。b2后R的A计数3，只有w报价4，派生A计数3、金额24，再加Q空行1，总4行/未报价1。b3后A与Q均无报价，总4行/未报价4/金额0。重放已接纳b2应返回其原切面，不得在b3之后再次执行其增量。源切面与派生结果必须分开绑定，历史initial不变。
