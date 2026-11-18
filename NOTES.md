# 715 — 独立观测语义服务与批次质量报表库

纯 Python 3.11 标准库实现，SQLite 存储。只处理**合成样品**与题定规则，不构成任何实际健康判断。

> 核心立场：`低于检出限5` 不是实测 2.5；`缺测` 不是 0。观测值是**非负实数域中的集合**，
> 全部用 `fractions.Fraction` 精确表示，推导不使用浮点，也不用中点/有限候选点代替连续区间。

## 目录

```
obs_service/
  semantics.py   # 独立语义：解析、规范化、精确区间代数、交集、阈值判定、均值分析
  store.py       # 版本化登记（测量/阈值）、不可变报表、逐样品贡献快照
  api.py         # 入口：导入、语义查询、报表、解释、operations 运行器
  cli.py         # 命令行入口
  demo.py        # 运行 materials/715 场景并断言题面结果
tests/
  test_semantics.py         # 48 项精确语义/报表测试中的语义部分
  test_service.py           # 715 场景、版本、快照隔离、1000 样品
  test_finiteness_sanity.py # 有限网格仅用于找反例（明确不是连续域证明）
NOTES.md          # 本文件
```

## 1. 观测语义

支持：`EXACT(v)`、`LT(u)`、`LE(u)`、`GT(l)`、`GE(l)`、`BETWEEN(l,u,两端开闭)`、`MISSING`。
端点是非负有理数；JSON 数字只能是**整数或分数字符串**（如 `"10/3"`），浮点会被拒绝。

规范化后的**非空连续区间**：

| 观测 | 规范范围 | 说明 |
|------|----------|------|
| `EXACT(v)` | `[v,v]` | 单点 |
| `LT(u)` | `[0,u)` | `u>0`；`LT(0)` 为空 → **拒绝** |
| `LE(u)` | `[0,u]` | `LE(0)` 规范化为单点 `[0,0]`（即 `{0}`，非空） |
| `GT(l)` | `(l,+∞)` | 上方无界 |
| `GE(l)` | `[l,+∞)` | 上方无界 |
| `BETWEEN(l,u,lc,uc)` | `(l,u)` 等 | 等端点仅当两端都闭才非空，否则拒绝 |
| `MISSING` | —— | **不是区间**，是"无观测"，与任何已测范围都不同（包括 `{0}`） |

- **输入观测本身不接受空范围**；只有*交集*查询可以返回空（`None`/`{"empty": true}`）。
- **交集**：对两区间逐端取较紧者，等值端点取"闭"的交（都闭才闭）；再判空。
- **阈值判定** `x > T`（T 为非负有理数）：
  - `x>T` 必然成立 ⇔ `range ∩ (T,+∞) ≠ ∅` 且 `range ∩ [0,T] = ∅`
  - 必然不成立 ⇔ 前者空且后者非空
  - 其余为不确定。
  用精确交集实现，天然处理连续域和开闭端（例如 `GE(5)` vs `T=5` 为不确定，因为 `x=5` 不满足 `>5`）。

## 2. 均值（仅各样品取值范围，不假设相关性/概率/分布）

对**非缺测**样品取算术平均。各自独立的区间之均值是盒式区间：

- 下确界 = `Σ lo_i / n`，下界可达 ⇔ **所有** `lo_i` 端都闭合（`lo=None` 表示 0，闭）。
- 上确界 = `Σ hi_i / n`（任一 `hi=None` 则上方无界 +∞），上界可达 ⇔ 有限且**所有** `hi_i` 端都闭合。
- 无非缺测记录 → 均值 **undefined**。
- 是否"唯一确定" = 范围为单点。

715 验证：
- r1：`LT(5),[5,5],(5,+∞)` + MISSING，n=3 → `(10/3,+∞)`，下确界**不可达**（s3 必须 `>5`），上方无界。
- r2：s3 更正为 `EXACT(7)` → `[4,17/3)`，下界可达（s1=0,s2=5,s3=7），上界**不可达**（s1 上端开）。

## 3. 版本与批次登记（store.py）

- 测量按 `(batch_id, project, sample_id, revision>0)` 存储，**只追加**。
- **重放/冲突**：同版规范语义相同 → 重放（接受，不变）；不同内容 → `RevConflict`。
  规范语义用于比较（`LT(5)` 与 `LT(10/2)` 视为同语义）。`MISSING` 与任何已测范围不同。
- **采用**：每 `(sample, project)` 取最高修订；低版合法但被忽略；更正为 `MISSING`
  表示本次缺测，**修订历史保留**。
- **整批原子性**：先全部校验（含 `LT(0)` 等空范围），任一非法/冲突则**整批拒绝**，
  一个事务内写入，绝不部分更新（715：`s2` 不会变成 1）。
- **阈值**有显式修订，且不可覆盖。

## 4. 不可变报表与贡献（存储选择与代价）

报表创建时**一次性**经语义服务解析采用修订与阈值，把以下内容物化进
`reports` / `report_contributions`：

- 汇总：总样品、有效、确定超标、可能超标（含确定）、缺测；均值边界与可达性。
- 逐样品贡献：**采用修订号、规范化范围、阈值判定、平均分母 n、上下界贡献**、可达性、可读解释。

**如何避免污染旧报表**：
- 报表读取只读快照表，从不重读当前测量或当前阈值，也不拿新阈值重跑判定。
- 阈值值以文本精确存入报表行；测试覆盖"导入新测量/新阈值后旧报表逐字节不变"及"关闭重开后不变"。
- `superseded` 只是方便标记，从不改动已存报表。

**代价 / 存储**：
- 每个报表按样品物化一行贡献（O(样品数)）。1000 样品即有 1000 行，可接受；
  更大规模可按报告分表/压缩，但语义不受影响。
- 端点以十进制/分数文本存储，保留 `+∞` 与开闭标志，故不能事后精确重算的"结点"
  （无界、开端点）不会丢失——这正是完整范围工件优于单一浮点汇总之处。
- 可选替代方案：不物化完整工件、只存"稳定语义表示 + 修订引用"，查询时精确解释。
  代价是旧报表的可解释性依赖**历史解释器/语义代码**仍能重现；本实现选择物化，
  换取旧报表在代码演进后仍可逐样品回读（重算代价前移到创建时）。

## 5. 入口

CLI：
```
python -m obs_service.cli --db obs.db import   --file materials/715/observations.json
python -m obs_service.cli --db obs.db report   --id r1 --batch b1 --project p --threshold 1
python -m obs_service.cli --db obs.db show     --id r1
python -m obs_service.cli --db obs.db explain  --id r1            # 均值推导，可手算核对
python -m obs_service.cli --db obs.db explain  --id r1 --sample s3
python -m obs_service.cli --db obs.db ops --file materials/715/operations.json \
                                           --import-file materials/715/observations.json
python -m obs_service.cli --db obs.db normalize --obs '{"kind":"LT","upper":"5"}'
python -m obs_service.cli --db obs.db intersect --a ... --b ...
python -m obs_service.cli --db obs.db check     --obs '{"kind":"GE","lower":"5"}' --threshold 5
```
Python：`ObservationService` 的 `import_dataset / import_measurements / register_threshold /
normalize / intersect / threshold_check / create_report / read_report / explain /
revision_history / run_operations`。

演示：`python -m obs_service.demo`（或 `--db /tmp/x.db` 演示关闭重开）。

## 6. 测试

```
python -m pytest -q          # 48 passed
python -m obs_service.demo
```

- `explain` 会**重建** `Σ贡献 / n` 并与存储界比对，不一致即报错，保证可手算追溯。
- `test_finiteness_sanity.py` 中的有限网格**只用来找反例**（验证采样点不越界、
  闭下端可达），文件头明确声明它不能证明连续域结论；界与可达性由精确算术单元测试给出。

## 7. 边界与不做的事

- 不引入统计估计、插补、单位换算、多变量约束（即使到 1000 样品）。
- 不假设样品间相关性/独立分布；均值只是"独立区间之盒"。
- 不做任何健康/合规判断。
