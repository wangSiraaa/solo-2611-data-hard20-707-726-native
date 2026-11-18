# 独立有序编组与货位卸货服务（materials/734）

全部为合成车辆，用 Python 3.11 + SQLite（标准库 `sqlite3`）实现，不控制真实列车。
没有第三方依赖，`fractions.Fraction` 保证长度/坐标全程精确分数，没有浮点误差。

```
rail/
  model.py        纯函数：Fraction 解析、规范哈希、编组序列/朝向/前缀和、接端/拆出规则
  db.py           SQLite schema、BEGIN IMMEDIATE 写事务、单调 seq
  composition.py  编组服务：车辆/编组注册、四种接端、head/tail 拆出、reverse、事件幂等
  unload.py       站台扫描（platform-scan/v1）+ 卸货作业/收据 + LOADED/UNLOADED 状态
  material.py     载入 materials/734 并重放 operations.jsonl
  store.py        一个连接、四个服务共享同一事务边界
  cli.py          railctl 命令行
tests/test_rail.py   48 个 unittest（标准库，无 pytest 依赖）
demo.py             断链场景与行为演示，写出 reports/R1.json、reports/R2.json
```

运行：

```bash
python3 -m unittest discover -s tests -v     # 48 tests, OK
python3 demo.py                              # 演示 + 生成 reports/
python3 -m rail.cli --db /tmp/rail.db init --material materials/734
python3 -m rail.cli --db /tmp/rail.db replay --material materials/734
python3 -m rail.cli --db /tmp/rail.db read-report R1 > R1.json
python3 materials/734/platform_reader.py R1.json
```

## 1. 数据结构选择：物化前缀和

每次编组版本变化都把整列顺序物化为 `train_cars(train_id, seq, car_id, orientation,
start, end, active)`，即每厢在列车轴上的半开区间 `[start, end)`。
- 端部操作（couple/split/reverse）本来就是 O(n) 的整列变化，物化成本可接受；
- 读取（scan / 坐标查询）是 O(k)（k=该列货物数），不需要沿连接结构回溯；
- `start/end` 用整数分子分母存储，读取时用 Fraction 精确重建，避免“分数前缀和”每次重算。

`trains.composition_version` 是顺序+朝向+前缀和的 SHA-256；`trains.revision` 是整数
单调修订号。**版本号相同不代表修订相同**：reverse 两次版本回到原值，但 revision 已 +2；
卸货作业同时绑定版本和 revision，所以“reverse 再 reverse”也不会让旧作业复活。

## 2. 几何

车厢永久身份只有 `(car_id, length)`；`local_position ∈ [0, length]`。

```
orientation=+1: x = start + local
orientation=-1: x = start + (length - local)
```

- couple 保持 target 自身正方向。四种接端：
  `尾接头 = target+other`，`尾接尾 = target+reverse(other)`，
  `头接尾 = other+target`，`头接头 = reverse(other)+target`；
  `reverse` 同时反序并翻转每厢 orientation。
- split 明确 head/tail 的 n 厢，`0 < n < 总厢数`；拆出序列保持拆前头到尾的方向，
  拆出部分按请求的 `new_train_id` 新建活动编组，原列保留剩余部分。两个新/旧编组
  各自从自己的头重新计算 `start`，但每厢 orientation 不变。
- 存储层用部分唯一索引 `UNIQUE(train_id,seq)` + `UNIQUE(car_id) WHERE active=1`
  保证“一厢不能同时属于两个活动编组”；不依赖应用层自觉。

## 3. 事件幂等

`event_id` 存请求语义哈希：同 id 同内容直接返回当时成功结果；同 id 异文抛
`ConflictError` 且不改任何状态。写入在 `BEGIN IMMEDIATE` 内二次检查，重复/并发都安全。
`split` 会消耗 `new_train_id`：重放同一事件返回原结果，不会再次拆出。

## 4. 扫描（platform-scan/v1）

`scan_platform(train_id, report_id, [start,end), 可选 composition_version)`
- 半开区间 `start <= x < end`；
- 可选版本必须等于当前活动版本，否则拒绝，避免拿旧版本扫新编组；
- 报告写入 `reports` + `report_positions`，**扫描只登记观察事实**，
  不改任何 cargo 状态；同一 report_id 同内容复读，异文拒绝。
- 旧报告永久保留：R1/R2 各自绑定自己的 `composition_version`，重开数据库后不变。

`reports/R1.json`、`reports/R2.json` 已按材料写出，可直接交给
`materials/734/platform_reader.py`：

```
R1 T 15047ba7…
C1 V1 2 IN
C2 V2 12 OUT
C3 V3 28 OUT
R2 T 6eda132d…
C1 V1 28 OUT
C2 V2 18 OUT
C3 V3 2 IN
```

## 5. 卸货作业

状态机 `PREPARED -> CONFIRMED` 或 `PREPARED -> CANCELLED`；CONFIRMED/CANCELLED 终态。

- `prepare_unload(job_id, train_id, composition_version, window, cargo_ids)`
  校验：编组存在且活动、版本匹配、每件 LOADED 且属于该列、每件当前 x 落站台，
  然后保存 job 身份、版本+revision、区间、去重后的 cargo_ids 以及每件的坐标依据。
  不修改 LOADED。同 job_id 同内容复读原准备/终态，异文拒绝。
- `confirm_unload(job_id)` 在**单个写事务**内重新核验：编组仍活动且版本+revision 未变、
  每件仍 LOADED、仍属于该列、当前坐标仍全部落站台。任一不满足整单拒绝，所有货物状态
  不变；全部满足才用带 `WHERE status='LOADED'` 的 UPDATE 逐件翻转，并写卸货收据。
  因此两个作业争同一件最多一个成功（第二个的 LOADED 条件不成立 → 整单回滚）。
- 已确认不能被取消；取消未确认作业后不能确认。已 UNLOADED 的货物没有任何 API
  能改回 LOADED，split/couple/reverse 也不会触碰 cargo 状态。
- `prepare` 依据绑定 `composition_version + revision`，split/couple/reverse 任一发生
  都会使旧依据失效；仅显示版本相同（reverse 两次）也不行，必须新建作业。

## 6. 断链场景（demo.py 实测）

1. E1 尾接尾得到 `[V1+, V2+, V3-]`，C1/C2/C3 全列坐标 2/12/28；R1 的 `[0,5)` 只命中 C1。
2. 站台 `[0,5)` 准备 U1 卸 C1：合法，C1 仍 LOADED。
3. E2 reverse 得 `[V3+, V2-, V1-]`，C1/C2/C3 坐标 28/18/2；旧 U1 confirm 被拒，C1 仍 LOADED。
4. 以新版本准备 U2 卸坐标为 2 的 C3 并 confirm 成功，C3=UNLOADED，收据 `RC-U2`。
5. 新 U3 再卸 C3：几何仍命中 `[0,5)`，但因 C3 已 UNLOADED，prepare 即拒绝。
6. R1、R2、U2 收据同时可查，三套记录分表保存，互不覆盖。

## 7. 规模与验证

- 上限规模压测（50 车厢 / 10 活动编组 / 500 货位 / 200 编组事件 / 500 卸货作业）：
  本机约 0.02s（事件）+ 0.1s（作业），远低于上限。
- 原子性：confirm 中途让收据表写入失败，事务回滚后 C1 仍 LOADED、作业仍是 PREPARED。
- 重开：文件数据库关闭再打开，编组版本、货物绑定、报告、事件复读结果均保持一致。
- 不做最优编组、时刻表、质量/制动计算。

## 8. 复杂之处与取舍

- **物化 vs 连接结构**：选物化前缀和。端部操作 O(n) 重写整列，换来 scan 的 O(k)
  读取和“版本即快照”的直接核验。若端部操作远多于读取，可改惰性连接结构，但需要
  额外的版本游标；这里读多写少、且要求报告与作业依据强绑定版本，物化更直接。
- **状态与依据分表**：报告表只记观察，卸货表只记授权/结果，cargo 表只记
  LOADED/UNLOADED 与最后卸货作业。三者不合并成一张“快照表”，避免“扫到即卸”。
- **revision 与 version 双绑定**：只用内容哈希会出现“改动了又改回去”的漏洞，
  加了单调 revision 作为作业依据的一部分。
