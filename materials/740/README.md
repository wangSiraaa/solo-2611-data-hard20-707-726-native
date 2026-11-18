# 740 临时名、全局身份和互引

input.json提供两个合法批和一个坏批。local p/d在b1与b2是两个命名空间，绝不能等同于跨批稳定ID。b1先员工、后部门，两者必需互引合法；b2倒序仍合法。坏批P-BAD/缺local nobody应整体拒绝，包括映射产品不可见。

内部ID的字面不规定。set_department_leader操作先按公开business_key查询此前回执/目录中的真实稳定ID，再提交b3更正，不是把business_key直接当内部ID。b3后D1负责人指向P2，P1仍归属D1。重放b1返回最初映射，不能因为b3又把负责人退回P1。此材料没有预分配目标ID或旧组织库。
