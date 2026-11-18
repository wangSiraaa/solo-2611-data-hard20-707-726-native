# 场馆晋级席位工具

现有 Python 3.11 命令支持查询每场最新比分、首次发席、记录席位已开始和读取一次发席回执，文件都在此目录。输入全为合成数据。从仓库根目录执行：

```sh
work=$(mktemp -d)
python3.11 materials/777/board_cli.py rankings materials/777/scores-v1.json
python3.11 materials/777/board_cli.py first "$work/venue.json" R1 materials/777/scores-v1.json materials/777/slots.json
python3.11 materials/777/board_cli.py read "$work/venue.json" R1
python3.11 materials/777/board_cli.py started "$work/venue.json" E1
python3.11 materials/777/board_cli.py rankings materials/777/scores-v2.json
```

第三条业务命令读到 `{"E1": "Pine", "E2": "Quartz"}`。改判比分各取修订2，得到 North=Cedar、South=Lime，即使 North 的修订1更晚出现在文件里。在空席位库上 `first` 可以用该成绩文件首次发布，已有正式回执时 `first` 仍只支持原请求复读。

随附的 `legacy-r1.json` 是当前版本的 `first` 生成的持久状态，可直接交给 `read`，无需重建。

新增勘误发布的样例使用两份各自存着同一 R1 的席位库。两席都未开始时，勘误 R2 应同时将 E1/E2 变为 Cedar/Lime，旧命令读取 R1 仍是 Pine/Quartz。E1 已开始的一份库面对同一勘误应拒绝整单，E2 也保留 Quartz，R2 回执不存在。以独立的调用重新打开文件检查结果。
