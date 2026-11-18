# 地址目录与独立投递作业库

Python 3.11 + SQLite（标准库 `sqlite3`），只处理合成地址。实现位于
`address_delivery/`，测试位于 `tests/`，不修改 `materials/707`。

## 快速开始

```bash
# 初始化 r1 目录与覆盖
python3 -m address_delivery init --db /tmp/a.db --materials materials/707

# 直接读 CSV（结构错误时整批拒绝）
python3 -m address_delivery import --db /tmp/a.db materials/707/addresses.csv

# 或接通给定客户端的 address-input/v1 输出
python3 materials/707/csv_client.py materials/707/addresses.csv \
  | python3 -m address_delivery ingest --db /tmp/a.db -

python3 -m address_delivery apply-revision --db /tmp/a.db materials/707/revision.json
python3 -m address_delivery retry --db /tmp/a.db P1 P3 P4
python3 -m address_delivery replay --db /tmp/a.db P2
python3 -m address_delivery submit --db /tmp/a.db P7 C1 西 东路 2

# 查询入口
python3 -m address_delivery resolve --db /tmp/a.db C1 同区 东路 2
python3 -m address_delivery explain --db /tmp/a.db P3
python3 -m address_delivery job --db /tmp/a.db P2
python3 -m address_delivery catalog --db /tmp/a.db street --id S2
```

测试：`python3 -m unittest discover -s tests`（78 项，覆盖下述全部场景）。

## 结构

| 模块 | 职责 |
| --- | --- |
| `normalization.py` | 仅 NFC + 首尾去空白；身份键构造 |
| `catalog.py` | 城市/区/街道稳定 ID、作用域名称表、完整父链解析、覆盖片 |
| `delivery.py` | 独立投递作业库：尝试、原子提交、身份冻结、历史 |
| `service.py` | 共享一个连接的组合入口（目录只读 + 作业写入同一事务） |
| `input.py` | CSV 与 `address-input/v1` 的结构校验 |
| `cli.py` | 命令行 |

**目录与作业彼此独立**：`Catalog` 只建目录表，`DeliveryStore` 只建
`jobs`/`attempts`；可以只打开目录库完成查询与批量解析，不需要任何作业表
（见 `tests/test_catalog.py::test_catalog_works_without_delivery_tables`）。

## 关键语义

### 名称作用域与解析

- 名称/别名分别存在 `district_names`、`street_names`，**只在声明的父级内有效**。
- 解析始终走完整父链：先按 `city_id` 限定区文本得到区集合，再只在这些区内
  按街道文本取候选。**不会先任选一个区**。
- 候选数 1 = `unique`；0 = `unknown`；>1 = `ambiguous`，并列出全部并列候选。
- 规范化仅 NFC + trim。`学  园`（两空格）与 `学 园`（一空格）是不同名称；
  同名文本可合法指向多个实体，**不是目录错误**。
- 停用街道仍进入候选集、仍可按稳定 ID 查询、仍解释历史名称；只有“新派送”
  一步拒绝。

### 覆盖与站点

- 命中条件：`lo <= n <= hi` 且 `any` 或奇偶匹配，**端点包含**。
- 同一门牌命中**多个不同站点** → `failed_station_ambiguous`。
- **同一站点的重叠覆盖片不构成歧义**（P3 在 r1 命中 K1+K2 均为 A → 不歧义；
  另命中 K3=X 才歧义）。

### 提交与身份冻结

作业在同一 `BEGIN IMMEDIATE` 事务内：查冻结记录 → 解析地址 → 查覆盖 →
写 `jobs`/`attempts`。成功需要：地址唯一、街道有效、站点唯一。

- 保存原文本、稳定街道/区、门牌、站点、目录版本、实际解释（候选+覆盖）。
- **首次成功后身份冻结**：再次提交内容相同（城市 ID + NFC/trim 的区/街道文本
  + 整数门牌）→ 原样复读当时结果，不重新解析目录；内容不同 → `rejected_identity_conflict`，
  已存作业不被改动。来源文件/行号不参与身份。
- 业务失败**不生成**已提交作业；失败包裹可重试并反映当前目录。
- 已成功的包裹即使改名、停用或覆盖修订，也不重派；旧失败无法覆盖已成功记录。

### 输入契约

- CSV 列名须**恰好**为 `parcel_id,city_id,district_text,street_text,house_no`；
  缺列/多列/字段数错/门牌非“正 ASCII 十进制整数” → 整批拒绝，且无任何副作用
  （测试含失败后重开验证）。
- `address-input/v1` 被直接接收，容忍新增字段，保留现有字段含义。
- 带引号逗号（`"学,园"`）、分解重音（`Café路`）按 NFC 正确匹配；
  内部空白不做合并。

## 更新与缓存失效取舍

- **按父链求交**：每次解析用 3 条索引查询（区名、街道名、区集合），
  规模 ≤5 城/50 街道时开销可忽略，且天然无缓存失效问题。
- **作用域索引**：`district_names(text)`、`street_names(text)`、`streets(district_id)`、
  `coverages(street_id)` 支撑“名称→实体”与“父链求交”。解析总是在当前事务内读，
  修订提交后**无需显式失效**。
- 未引入跨修订的内存缓存：目录极小而变化会改变候选集与站点，缓存收益低、
  失效风险高。若未来目录变大，可在 `catalog_meta.version` 上做键控缓存，
  版本变更即整体失效；已提交作业因冻结身份不受影响。
- 修订原子：任一操作失败则整个修订回滚（版本号也不推进）。

## 材料核对结果

| 包裹 | r1 | r2 后 |
| --- | --- | --- |
| P1 | 两个街道候选（S1/S2）→ 歧义 | 仍歧义（不因停用/覆盖猜地址） |
| P2 | S3 → A 提交 | 复读 r1 冻结事实 |
| P3 | 门牌 9 命中 A/X → 站点歧义 | K3 删除后 → A 提交 |
| P4 | 街道未知 | 别名“东部支路” → S1 提交 |
| P5 | 引号逗号 → S3 提交 | 复读 |
| P6 | 分解重音 NFC → S3 提交 | 复读 |
| P7 | — | 西/东路 = 停用 S2 → 拒绝新派送 |

最终已提交：P2/P5/P6（版本 1）+ P3/P4（版本 2）；P1、P7 无作业。
