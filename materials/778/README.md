# 旧标尺报文工具

Python 3.11，无依赖。目录接口读取一个完整修订，旧发布器向一个接收端输出零原点、单位刻度的报文。安装在终端上的原接收器读取 `rail-scale-v1` 格式，按自己的档位、原点和刻度限制验读。以下命令从仓库根目录运行：

```sh
work=$(mktemp -d)
python3.11 materials/778/publish.py catalog materials/778/positions-v1.json
python3.11 materials/778/publish.py legacy materials/778/positions-v1.json materials/778/receivers.json "$work/old.json" East L0 L2
python3.11 materials/778/receiver.py "$work/old.json" materials/778/receivers.json East
```

读回 `{"L0": 0, "L2": 2}`。随附文件 `legacy-east.json` 是用这套旧命令发布的产物，也能直接交给接收器。

新请求 `request.json` 给出两个接收端的取数清单。位置修订1的东西两端各自都能精确编码（原点12或14、步长3），但共同编码无解。共同步长必须整除所取位置间的差，故最多为2，全范围8毫米至少需4个刻度。修订2只更正 B0、B1，此时取原点12、步长3可以共同发布，East 读回 A0=12、A1=18，West 读回 B0=15、B1=18。旧输出应保持可读。
