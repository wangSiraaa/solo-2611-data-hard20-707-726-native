# 合成音频进度客户端

在本目录运行 `python3 reader.py chapters.json listened.json A`，输出 `{"chapter_id":"A","next":5,"completed":true}`（JSON 空格可不同）。B 当前的首缺位置为 5，即使已有 [7,9)。`changes.json` 是后续真实输入；R2 后 A 首缺 5，补 E3 后 A 完成且 B 首缺 9。现有代码只读取单版目录以及一段覆盖章首的旧听取记录。
