# 成套放行旧工具

从任务仓库根目录执行。Python 3.11 标准库即可。以下在临时目录写入旧预检和台账副本。

```sh
work=$(mktemp -d)
cp materials/780/journal_v1.json "$work/journal.json"
python3 -B materials/780/calibration.py materials/780/catalog_v1.json materials/780/samples.json L-in
python3 -B materials/780/release.py prepare materials/780/catalog_v1.json materials/780/samples.json materials/780/requests/in.json --output "$work/in.json"
python3 -B materials/780/release.py confirm materials/780/catalog_v1.json materials/780/samples.json "$work/in.json" --output "$work/journal.json"
python3 -B materials/780/release_reader.py "$work/journal.json"
```

旧版 `L-in` 用 `L-default` 得 11；左右测量 11 和 12，可以预检并确认，历史汇总为 `kit-in=23`、`released-0=21`。

验证修订时，用一份新的初始台账副本。先用 v1 为 `kit-in`、`kit-edge` 各生成一份真实预检文件但不确认，再以 v2 确认这两份旧格式预检。`kit-in` 过期；以 v2 重做预检时，左右各自仍为合法校准结果，但差值 3 超过成套上限，不能通过。`kit-edge` 的左样本在 [10,20) 的右端点，仍用旧默认行、结果不变，以 v2 直接确认旧预检成功。旧读取工具显示 `kit-edge=23`、`released-0=21`，没有 `kit-in`。
