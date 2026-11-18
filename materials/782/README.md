# 转接交付客户端

在本目录运行 `python3 release.py initial.json`，应得到 start A、end C、steps E1/E2/E3。旧版交付按输入行序核连续。changes.json 给后续两批；R2 合法入仓但不能交付，R3 将两个孤立的转接移到主链后可交付，正确顺序为 E1/E2/E3/E4/E5。
