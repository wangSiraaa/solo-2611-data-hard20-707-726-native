# 成对重复证据与禁止同簇的归并发布

input.json 是初始切面与操作。sAC 是禁止证据。v1 依次处理 AB、BC、CD，BC 的端点没有被直接禁止，但已有簇 {A,B} 与 C 的 A–C 证据阻止合并。CD 不受拒绝路由影响。P1 查询 B/D 得 false。sAC 高版撤回后 P2 得真，P1 的结果仍为 false。
