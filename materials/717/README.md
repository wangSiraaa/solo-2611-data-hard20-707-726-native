# 717 模板与保存计划读取合同

这里提供原文模板/请求/修订、初始化SQL和只读plan_reader.py，不包含编译器或完整执行库。schema.sql可用sqlite3.executescript建立空notes表。按requests.json中的batches顺序执行；operations.json再修订put、执行新批并回读旧批，最后给一条多余键拒绝例。

计划格式sql-param-plan/v1必须含format、template_id、revision、source、sql和parameters。parameters按出现顺序，每项为name、start、end，位置是原文的Unicode字符[start,end)，包括冒号。不能按UTF-8字节数算；引号、换行和注释不改变。reader-example-plan.json是外部手写的小合法产物，含中文、注释冒号和重复id；reader-example-request.json给id=7。它仅用于演示读取合同，不是目标模板的编译答案。

`python3 plan_reader.py reader-example-plan.json reader-example-request.json`读取保存文件并输出sql及顺序绑定；公共函数load_bound_plan(plan_path, request_path)返回(sql, tuple)，执行模块可直接复用。客户端不解析SQL、不开数据库、不代替目标编译器。请求固定template_id/revision；bindings支持null、64位有符号整数、字符串以及{"$bytes":"十六进制"}，拒绝bool和其他结构。计划本身可没有参数。

正常批应插入id=1、message="hello:literal"、payload=00ff；get返回重复id绑定的echo=1。失败批先试插id=2，再因id=1重复失败，因此id=2不能落库。修订后id=3保存"new:revised"，原批仍能查到旧计划及":literal"来源。实际执行不能重新扫描source；编译后的sql原文引号/注释要保持。
