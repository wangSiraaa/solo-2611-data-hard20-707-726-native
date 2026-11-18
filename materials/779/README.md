# 导出预约旧工具

从任务仓库根目录执行。Python 3.11 标准库即可。命令操作临时台账副本。

```sh
work=$(mktemp -d)
cp materials/779/journal_v1.json "$work/journal.json"
python3 -B materials/779/route_catalog.py materials/779/catalog_v1.json A
python3 -B materials/779/daily_summary.py "$work/journal.json"
python3 -B materials/779/booking.py materials/779/catalog_v1.json "$work/journal.json" materials/779/requests/conflict.json
python3 -B materials/779/daily_summary.py "$work/journal.json"
```

旧目录返回 A 的速率 6。旧汇总初始显示 2 单、56 速率分钟。旧确认逻辑接受 `joint-conflict`，因为它只检查 B 路。再次汇总为 3 单、74 速率分钟。

用初始台账的一个新副本验证共享组时，`joint-conflict` 在共享容量变化前的 [12,14) 恰好满足 7，从 14 起需要 7 而只准 6，整项拒绝，旧汇总仍为 2 单、56 速率分钟。随后 `joint-edge` 在时刻 20 不再与 A 的旧项相交，它与 B 的旧项合计 6，确认后旧汇总为 3 单、64 速率分钟。重放该确认仍为 3 单。
