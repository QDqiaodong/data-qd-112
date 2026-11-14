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

## 站点发车供给集市（service-path-supply）

把 v1 快照按自然日区间展开成 SQLite 供给集市，回答"哪天、哪个站、哪个小时有多少班车、
最大多久没车"：

```sh
.venv/bin/service-path-supply --source fixtures/supply/timetable.json \
    --start 2026-01-01 --end 2026-01-05 --database run/supply.sqlite
```

- 输入是 v1 快照、起止自然日期（`YYYY-MM-DD`，含首尾）与 SQLite 路径；快照里的
  `query` 照常校验但不限制结果，日期完全由 `--start`/`--end` 决定。
- 只有非末站 call 计为发车（末站没有后续离开）；按日历与 `added`/`removed` 例外逐服务日
  判断启用班次，例外可在常规范围外、可跨区间。
- `00:00:00`–`47:59:59` 的服务时刻按 `时间 // 24h` 换算自然日、按 `% 24h` 换算日内秒；
  统计窗口的第一个自然日会纳入前一服务日跨过午夜的发车，窗口外自然日不写入。
- 表 `departures`：`(service_day, trip_id, call_index)` 唯一，另有 `natural_day`、
  `stop_id`、`day_seconds`（0..86399）。表 `hourly_departures`：主键
  `(natural_day, stop_id, hour)`，每个站点每个自然日 0..23 时都有行；`departure_count`
  为发车条数（同秒多车分别计数），`max_headway_seconds` 为该小时内去重后发车时刻与
  两端整点边界构成的相邻间隔最大值，空小时为 3600。
- 重跑只删除并重写 `natural_day` 落在请求区间内的分区，其他日期保持原样；事实与汇总
  在同一事务提交。快照校验或写库失败时整体回滚，旧库不变；首次写入失败不会留下数据库
  文件。输入数组乱序不改变逻辑结果。
- 建库为有界内存流水线：事实不物化进 Python，而是按固定 1000 行批次流式写入临时 SQLite
  暂存缓存；汇总按主键顺序把事实流读回来，逐小时维护去重发车秒（一小时至多 3600 个整数）
  并与"自然日 × 站点 × 24 小时"网格单次归并。除输入快照与 SQLite 缓存外，Python 额外
  内存不随事实或汇总总行数增长；发布时 `ATTACH` 缓存，在单事务内以
  `INSERT ... SELECT ... ORDER BY` 复制两表，提交后再 detach，暂存缓存始终被清理。
- 成功时向 stdout 打印汇总 JSON（自然日数、服务日数、站点数、发车数、汇总行数），
  失败退出 2 并保留旧库。

有界内存性能脚本（仅标准库，可复现）：

```sh
python3 scripts/perf_supply.py
```

同一固定快照分别在独立子进程跑 100 天与 3000 天窗口，`tracemalloc` 覆盖建库全过程；
长窗口物化 ≥200 万事实、≥70 万汇总行，打印两窗口的行数、耗时与峰值（MiB），并断言
两次峰值差 ≤32 MiB（当前实测两窗口峰值均约 1 MiB、差值约 0.01 MiB）。`--days 短 长`
可改窗口，`--keep-db` 保留数据库，阈值未达退出 1。

样例 `fixtures/supply/timetable.json` 含同秒双车、跨午夜、24:00 右边界、停运日、
范围外新增例外与仅周末班次，全部为原创虚构数据。

已知限制：只使用标准库与 SQLite；不处理实时延误、DST、步行或跨 trip 贯通车辆；
单次区间最多 3660 个自然日、汇总最多 500 万行，也不对数据库做跨进程并发写入协调。
