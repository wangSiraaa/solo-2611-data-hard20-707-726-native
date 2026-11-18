# 领退料历史与读取客户
history.sqlite包含已领I_OLD：需求A两单位用B四件，k=2；R_OLD已退一需求单位并返B两件。当前B库存6、W1/L1净满足1，是扣除与返还后的状态。catalog.json给当前直接合同，revision.json将A←B的k改3；未出现的合同保持原状。

运行`python3 ledger_reader.py history.sqlite`查看历史；服务复制库后推进operations.jsonl，保留此客户读取含义，可加表/列或兼容视图。

手算：R_REST仍用旧k=2，B从6到8，W1/L1满足1到0。I_OVER需B共(2+1)×3=9，大于8，整单失败，W2两行仍为0。I_NEW用B6，剩2，W2/L1净满足2。A←B、B←C不能推出A←C。重放R_REST不再返库。
