# data-qd-112 · 跨午夜公交路径查询

Service-day timetable routing with auditable transfer itineraries。数据流程与完整口径见 [docs/contract.md](docs/contract.md)。

## 安装

Python >=3.11，运行和测试仅使用标准库。先确认 `python3 --version`。
每个项目独立安装，不复用其他仓的虚拟环境或全局包：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
```

已具备相同版本的公共 wheel 缓存时，可用 `--no-index --find-links <wheel目录>` 安装构建工具；
否则首次安装可能联网。实际业务运行与测试无需网络、账号、Docker 或外部服务。

## 演示与测试

在仓库根目录执行；输出为可独立读取的 JSON，重跑全量重算、不累加。

```sh
.venv/bin/service-path --source fixtures/timetable.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

输入校验或计算失败退出 2，保留原输出；JSON 通过同目录临时文件完成后替换。
不对正在被其他进程写入的输出运行；不承诺文件系统断电恢复或在线并发发布。
修改源码后需重新安装。`run/`、`.venv/` 与构建产物均不入 Git。

样例来源见 fixtures/DATA-SOURCES.md。代码与自制样例 MIT；没有正式题面、评测答案或内部编排代码。

## 已有能力与样例结果

服务日23:50从A出发，经N1到B再换乘N2，24:15到D：到达87300秒、耗时1500秒、换乘1次。
`timetable.py`解析日历与跨午夜时间，`route.py`执行连接扫描并保留逐段行程。
不处理实时延误、步行边、DST或前后服务日混查；输出是静态数据查询结果。

## --pareto 模式

加 `--pareto` 输出最早到达与少换乘的全部非支配取舍（定义见 docs/contract.md）：

```sh
.venv/bin/service-path --source fixtures/timetable.json --output run/pareto.json --pareto
```

样例数据返回两个 journey：24:15到达（87300秒）、换乘1次的 N1→N2，与 24:30到达
（88200秒）、换乘0次的直达 N3；同一（到达，换乘）组合只保留逐段（trip,index)
字典序最小路径，数组按到达、换乘升序。不可达时 journeys 为空数组。
