# sql_param：独立 SQL 命名参数模板编译服务 + SQLite 批量执行库

纯 Python 3.11 标准库实现，不依赖第三方包，编译/绑定不需要数据库。

## 目录结构

```
sql_param/
  compiler.py    字符级状态机模板编译器（单引号/双引号/行注释/块注释/:name）
  bindings.py    值解码（null/int64/str/bytes/$bytes）与按出现顺序绑定
  plan_store.py  sql-param-plan/v1 计划发布、重放校验、持久化与公开格式校验
  executor.py    固定修订 → 整批预检 → 单事务依次执行 → 回执落盘
  errors.py      统一异常类型
cli/
  publish_plan.py   独立 CLI：发布单个模板为计划文件
  run_batch.py      独立 CLI：读取批次 JSON 执行并输出回执路径
demo_run.py        读取 materials/717 的完整端到端驱动（含与材料 plan_reader.py 互操作）
tests/             39 个 unittest 用例
```

## 词法规则（为什么不能用正则替换）

编译器逐字符扫描，五个状态：正常 SQL、单引号字符串、双引号标识符、行注释、块注释。

- 单引号字符串 `'...'`，`''` 为转义；双引号标识符 `"..."`，`""` 为转义。
- 行注释 `-- ...` 到换行；**允许在文件结尾终止**。块注释 `/* ... */`，不支持嵌套。
- 字符串、标识符、注释中的所有字符（含引号、空白、换行、`:`、`?`、`;`）**逐字符保留**。
- 只有正常区域里完整的 `:name` 是参数，`name` 为 ASCII 字母开头的字母数字下划线；
  替换为单个 `?`，并按 **Python Unicode 字符索引（代码点，非 UTF-8 字节）** 记录
  `[start, end)`，位置含冒号。重复名字产生多条参数记录。
- 正常区域的 `;`（包括末尾）、`?` 一律拒绝；`:` 后不匹配合法名字也**明确报错**，
  不静默吞掉。未结束的引号/块注释拒绝。
- 只接受一条 SELECT / INSERT / UPDATE；空模板、多语句、其它语句种类拒绝。
- 原文不做任何 Unicode 归一化。

## sql-param-plan/v1 计划格式

公开 JSON 格式，字段：`format`、`template_id`、正整数 `revision`、原文 `source`、
可执行 `sql`、按出现顺序的 `parameters`（每项 `name`/`start`/`end`）。
计划可零参数。该格式与 `materials/717/plan_reader.py` 的读取合同完全对齐，
材料脚本可直接消费本服务写出的计划文件（见 `demo_run.py` 第 2 步）。

## 绑定值

| JSON 表示 | Python/SQLite 值 |
| --- | --- |
| `null` | `None` |
| 整数 | 有符号 64 位范围 `[-2^63, 2^63-1]` |
| 字符串 | `str`（原样保留，不归一化） |
| `{"$bytes": "十六进制"}` | `bytes`，空串 = 空 BLOB |

- `true`/`false` 明确拒绝（bool 虽是 int 子类也不接受）；浮点、数组、其它对象拒绝。
- 绑定字典必须**恰含**全部不同参数名：缺键、多键都拒绝。
- 同名参数出现 N 次，按参数顺序复制同一个值 N 次。

## 计划兼容、更新可见性与连接复用边界

**兼容性**

- 计划一经发布即不可变。同 `template_id + revision + source` 重放是幂等的，返回同一计划；
  同 `template_id + revision` 但 source 异文直接拒绝；新修订必须递增 revision，写入新文件，
  永不覆盖旧计划。
- 存储布局：`<store>/plans/<template_id>/r<revision>.json`，纯 JSON/UTF-8，可独立拷贝归档。
  写入走临时文件 + `os.replace` 原子替换并 fsync。
- 限额：至多 100 个模板；单批至多 100 项。

**更新可见性**

- 执行侧每次都从磁盘重新 `load` 计划并按公开合同校验，不缓存编译对象、不重新扫描 source。
  因此新发布的修订对后续批次立即生效；已完成批次的回执内嵌当时完整计划快照（含 source
  与 sql），重开进程仍可回读，不受后续修订影响。
- 批次 ID 不可变：重复执行同一 batch_id 直接拒绝，历史回执不会被覆盖。

**连接复用**

- 每个 `execute_batch` 打开独立 sqlite3 连接，`BEGIN` 显式开事务，提交或回滚后在
  `finally` 中关闭；批次之间不共享连接、不共享事务。预检阶段完全不碰数据库。
- 初始化 DDL（如 `schema.sql`）由调用方用自己的连接 `executescript` 完成；
  模板本身不允许含分号，执行库永远只跑单条参数化 SQL。

## 批量执行语义

1. 每项固定 `template_id + revision`，从保存计划读取 `sql` 和参数顺序（绝不重编译）。
2. 开事务前整批预检：计划存在、绑定键集合精确匹配、值类型合法、语句为 SELECT/INSERT/UPDATE。
   预检失败抛 `BatchError`，不开事务、不写库、不产生回执。
3. 预检通过后在一个真实事务内依次执行：
   - INSERT/UPDATE 保存 `rowcount`；
   - SELECT 保存列名与全部结果行，带 `ORDER BY` 时按游标顺序保留行序（回执标记
     `order_by: true`），未带 ORDER BY 不承诺跨次行序；
   - 任一项 SQLite 约束失败立即 `ROLLBACK`，前序写入全部撤销，回执 `status` 为
     `rolled_back` 并记录失败项错误，落盘保存，绝不标成功。
4. 回执中的 bytes 以 `{"$bytes": hex}` 表示；`ordered_values` 按计划参数出现顺序
   （含重复名字），与 `plan_reader.py` 输出语义一致。

## 使用示例

```python
from sql_param import PlanStore, BatchExecutor

store = PlanStore("./workdir/store")
store.publish("put", 1,
    "INSERT INTO notes(id, message) VALUES(:id, :text || ':literal') /* :x ? ; */")

executor = BatchExecutor(store, "./workdir/notes.db", "./workdir/receipts")
receipt = executor.execute_batch("b-ok", [
    {"template_id": "put", "revision": 1,
     "bindings": {"id": 1, "text": "hello"}},
])
```

CLI：

```bash
python -m cli.publish_plan ./workdir/store put 1 'INSERT INTO t VALUES(:id)'
python -m cli.run_batch  ./workdir/store ./workdir/notes.db ./workdir/receipts \
        b-ok batch.json
```

## 验证

```bash
python3 -m unittest discover -s tests -v   # 39 个用例
python3 demo_run.py                         # 材料全流程 + plan_reader 互操作
```
