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
.venv/bin/service-path-import --source fixtures/gtfs/sample \
    --query fixtures/gtfs/query.json --output run/snapshot.json
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

## 离线 GTFS 导入（service-path-import）

在仓库根目录把一个 GTFS 目录转成 `service-path` 可直接读取的 v1 快照：

```sh
.venv/bin/service-path-import --source fixtures/gtfs/sample \
    --query fixtures/gtfs/query.json --output run/snapshot.json
.venv/bin/service-path --source run/snapshot.json --output run/report.json
.venv/bin/service-path --source run/snapshot.json --output run/pareto.json --pareto
```

- 读取 `stops.txt`、`trips.txt`、`stop_times.txt` 及 `calendar.txt`/`calendar_dates.txt`
  （两者至少一个），按标准列名取值；额外列忽略，支持 UTF-8 BOM、引号内换行与前导零 ID。
- `stop_sequence` 按整数升序排列且同一 trip 内唯一；`00:00:00`–`47:59:59` 保留服务日
  时制，单数字小时规范成两位；空时刻、非法时刻、到达晚于离开、下一站到达不晚于前站
  离开均拒绝。
- 星期日历与 `calendar_dates.txt` 增删例外合并；只有例外的 service 输出为空
  `weekdays` 与首个/末个例外日的跨度。同 service 同日重复的例外行拒绝。
- 所有引用（trip→service、stop_times→trip/stop、query 站点）、日期与停运 trip 的结构
  均校验。GTFS 没有换乘时间，导入后站点 `transfer_seconds` 统一为 0。
- 输出数组按 ID/序列排序，GTFS 行顺序变化不改变输出字节。失败时错误信息定位到文件、
  记录号、行号与关键 ID，退出 2 且不创建或覆盖旧输出（与 `service-path` 一样通过同目录
  临时文件替换）。
- `--query` 接受一个 query 对象，也接受含 `query` 字段的现有快照；只复用其中的
  date/origin/destination/depart，不改动原文件。

已知限制：`calendar.txt` 每个 `service_id` 只允许一行（v1 快照只有一个星期掩码与闭区间）；
GTFS 中同一 trip 的 `stop_sequence` 重复、或需要额外 `frequencies.txt`/`transfers.txt`
的场景不支持，遇到时按校验失败处理。
