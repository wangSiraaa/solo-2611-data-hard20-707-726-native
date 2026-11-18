# 测量质量库（qualitylib）

精确有理数的工业测量评价库，目录与样品数据解耦。参见 [DESIGN.md](DESIGN.md)。

## 快速开始

```bash
# 复放 materials/708 的 operations.jsonl（自动引导 r1 → 评价 → r2 → 拒绝坏批次 → 读旧报表）
python3 -m qualitylib.ops materials/708/operations.jsonl \
    --db var/quality.sqlite3 --workdir reports \
    --reader materials/708/report_reader.py

# 生成 708 报表 + 8 量/30 单位/500 测量合成数据，并用下游 reader 读回
python3 -m qualitylib.demo

# 测试
python3 -m pytest -q
```

## 目录

| 文件 | 作用 |
| --- | --- |
| `qualitylib/edge.py` | 有向仿射边 `y = a·x + b`、反向式、类别归一 |
| `qualitylib/directory.py` | 单位目录、闭路恒等校验、确定性路径、修订折叠 |
| `qualitylib/catalog.py` | 规范与测量的解析/校验 |
| `qualitylib/evaluate.py` | 类别感知评价（缺转换/类别错误不产生合格记录） |
| `qualitylib/db.py` | SQLite：版本化目录、目录/测量快照、不可变评价 |
| `qualitylib/service.py` | 服务层：引导、应用修订、评价、报表 |
| `qualitylib/reports.py` | `measurement-report/v1` 读写 |
| `qualitylib/ops.py` | `operations.jsonl` 运行器 |
| `qualitylib/synthetic.py` | 合成工业数据生成器 |
| `qualitylib/demo.py` | 一键生成全部演示产物 |

## 关键保证

* **精确**：全程 `Fraction`，无浮点容差。
* **类别隔离**：`absolute` 用 `a,b`，`difference` 只用 `a`，不可混算。
* **最终图校验**：修订先折叠成最终边集，再做闭路恒等校验；暂时冲突不拒绝，
  最终矛盾整批原子拒绝且当前版本不变。
* **证据留存**：每次评价快照规范、原测量、换算值、单位、路径、版本与指纹；
  重新评价新增版本，旧记录可解释。
* **报表契约**：保留 `evaluation_id`、`measurement_id`、`directory_revision`、
  `converted_value`、`passed`；不可换算项进入 `rejections`，不污染 `evaluations`。
