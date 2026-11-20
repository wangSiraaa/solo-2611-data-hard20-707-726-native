# 带字段及分段的倒排产品与短语入选

Python 3.11 标准库实现（仅用 `sqlite3` 等内置模块，无第三方依赖）的
**位置索引目录** + **素材清单发布服务**，用于处理有限合成英数字文本。

## 目录结构

```
index_service/
  tokenizer.py    ASCII 英数字词元化（小写化、下划线/非 ASCII 均为分隔符）
  catalogue.py    SQLite 位置倒排目录（按修订、字段、段、位置）
  phrase.py       精确位置短语匹配（同字段/同段/严格递增/最大夹词 g）
  publisher.py    切面感知的版本化发布（含入选与排除原因工件）
  service.py      目录 + 发布器门面
  runner.py       运行 materials/<id>/input.json 场景并输出报告
tests/            unittest 测试（tokenizer/catalogue/phrase/publisher/场景/模糊）
```

## 核心语义

**词元化**：每段内存连续 ASCII 英数字为一个词元；其它字符（含 `_` 与非
ASCII）皆为分隔符；连续下划线不产生词元。位置按**段内有效词元**从 0 计数，
不因分隔符占位。

**倒排目录**：每个词元映射到 `(document_id, revision, field, segment,
position)`。`field` 仅 `title`/`body`。每份修订的原文（分段列表）也保留。
目录本身不认识查询表达式、排除规则或发布版本。

**修订**：
- 更正整份替换：新修订成为 canonical，旧修订与其倒排**仍可查询**（按切面）。
- 同 `document_id` + `revision` + **同段序 + 同原文** 重放：幂等空操作。
- 同 `revision` 但异文：整批拒绝（`RevisionConflict`），事务回滚，无部分写入。

**短语匹配**：输入有序词列表与相邻词最大夹词数 `g`。要求全部同字段、同段、
位置严格递增、相邻位置差 `≤ g+1`。重复词必须用**不同**位置。匹配用段内
反向动态规划（线性于 postings 数），不是贪心，能正确处理重复词与回退。

**发布**：
1. 先在指定切面（默认当前 canonical 修订集）上求 `include` 短语（默认
   `title`）得到候选。
2. **完成完整查询匹配之后**，再用 `exclude` 短语（默认 `body`）排除候选——
   仅在 body 见到排除词的任意一个**不会**删除素材。
3. 仅当剩余**不同**素材 ≥ `minimum_documents` 时成功；否则抛出
   `PublicationRejected`，**不保留部分清单**。
4. 每份工件含：入选 `document_id`、`revision`、一组合法 `title` 位置
   （`segment` + `positions`），以及被排除素材的独立查询与位置。
5. 工件固化其切面（`cut`）；旧发布按旧切面读回，不受后续修订影响。
6. 复读已成功请求（同 id + 同版本 + 同查询）返回原工件；同 id/版本但查询
   不同则冲突报错。

## 场景 775 复现

```bash
python3 -m index_service.runner materials/775/input.json
```

结果：
- `P1`（v1）成功，入选 **D1 / D4**：
  - D1 `title` 中 amber@0、gear@2，夹一个 `bolt`，`g=1` 通过；body
    的 stop/blue/tag 不紧邻，不排除。
  - D2 两词跨段 → 不入选。
  - D3 body 有 `stop-tag`（相邻）→ 真排除。
  - D4 排除词在 title，不影响（排除只看 body）。
- 应用 D4 revision 2（body 改为 `stop tag`）后，`P2`（v2）仅剩 D1，
  低于 2 份 → **拒绝**，不落库。
- `read P1` 仍返回原清单 D1/D4（旧切面 D4 revision 1）。

## 测试

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

覆盖：词元化边界、段内位置、修订重放/冲突/批量原子性、切面查询、短语跨段/
同字段/顺序/重复词/夹词边界、发布入选与排除、修正后拒绝、旧清单读回、
持久化、以及 DP 匹配与暴力枚举的模糊对拍。

## 规模

合成上限约 100 文档 × 每文档 1000 词、查询词 ≤ 8；摄取与查询均在亚秒级
（见 `tests` 与本仓库基准）。
