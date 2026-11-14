# Service-day routing v1

输入是服务日时刻表快照：stops(id,transfer_seconds 0..3600)、calendars、trips 和一个 query。
日历 start/end 为闭区间 ISO 日期，weekdays 是周一0到周日6；added/removed 为日期例外且不交叠，
例外可在常规范围外。trip 包含唯一ID、calendar 和至少两个 calls(stop,arrival,departure)。
站点在同一trip中不重复；到达<=离开，下一站到达严格晚于前站离开。所有引用须存在。
即使停运trip也校验结构。不支持跨trip的贯通车辆或步行边。

时间为该服务日起算的 HH:MM:SS，小时00..47；24:15 属于该服务日次日00:15。
只查询指定服务日发起的行程，不自动混入前后服务日。此为无时区的合成墙钟时间轴，不处理DST。
query 含 date、origin、destination、depart。首次上车不加换乘耗时；中转按到达站transfer_seconds，
等于离开时间可上车；同一trip留在车上不用重新加换乘耗时。

连接扫描按离开秒、trip ID、段号排序；保留最早到达站点标签及已登车前缀。
只优化最早到达，不优化换乘数；同到达时保留先扫描路径，不声称Pareto最优。
源=目标返回空行程及出发时刻；不可达是正常结果reachable=false，而非解析失败。
输出逐段trip/站点/服务秒、到达、耗时和换乘次数。fixture A→B→D 24:15到达（87300秒），
总耗时1500秒、换乘1次；B恰好满足120秒换乘。A→C留在N1车上24:20到达。

## --pareto 模式

加 `--pareto` 后输出 `{'query':..., 'journeys': [...]}`，回答"最早到达"与"少换乘"之间的全部取舍。
每个 journey 含 arrival_seconds、elapsed_seconds、transfers 和与默认模式相同字段的完整 segments。
支配定义：行程X支配Y 当且仅当 X.arrival_seconds<=Y.arrival_seconds 且 X.transfers<=Y.transfers
且至少一项严格更优。journeys 覆盖全部非支配（到达，换乘）组合；同一（到达，换乘）组合只保留
逐段（trip,index）字典序最小的路径；数组按 （到达，换乘） 升序。不可达时 journeys 为 []；
源=目标时仅含一个零段零换乘行程（到达=出发、耗时0)。

中间站不按最早到达单标签剪枝：每站保留 （到达，换乘） 标签集，仅当另一标签到达不更晚且
换乘严格更少时才丢弃（其后续旅程必被支配）；换乘数相同但到达更晚的标签保留，因为最终同一
组合的字典序最小路径可能经过它。同车续乘免换乘缓冲、首次上车免换乘耗时、中转按换乘站
transfer_seconds、跨午夜与日历例外语义均与默认模式一致。校验失败仍退出2并保留旧输出文件。

当前为内存静态快照，不接实时GTFS接口、不做实时延误预测、交通运营或在线导航保证；
本地离线 GTFS 目录可通过下一节的导入命令转成快照。

## 离线 GTFS 导入 v1（service-path-import）

命令 `service-path-import --source <目录> --query <现有查询JSON> --output <快照>` 把本地
GTFS 目录转成上述 v1 快照，可直接交给 `service-path`（默认与 `--pareto` 一致）。只使用
标准库，不联网；已有 `service-path` 接口与快照字段不变。

输入表与标准列名：
- `stops.txt`：`stop_id`（必填）。
- `trips.txt`：`trip_id`、`service_id`。
- `stop_times.txt`：`trip_id`、`arrival_time`、`departure_time`、`stop_id`、`stop_sequence`。
- `calendar.txt`：`service_id`、`monday`..`sunday`（0/1）、`start_date`、`end_date`（YYYYMMDD）。
- `calendar_dates.txt`：`service_id`、`date`、`exception_type`（1 增，2 删）。
`calendar.txt` 与 `calendar_dates.txt` 至少存在一个。额外列忽略；文件可带 UTF-8 BOM；
CSV 双引号内的换行、逗号按标准处理；ID 保留原样（含前导零）。表头缺必需列即失败。

校验与转换：
- `stop_sequence` 必须为非负整数，同一 trip 内唯一；排序后按序列生成 calls。
- 时刻为 `HH:MM:SS`，小时 00..47；单数字小时规范为两位。空时刻、分钟/秒越界、
  小时>47、离开早于到达、下一站到达不晚于前站离开均拒绝。
- GTFS 日期按 YYYYMMDD 解析必须为真实日期；`start_date<=end_date`；星期标志只接受 0/1。
- 周日历与例外合并：`added` / `removed` 分别输出升序 ISO 日期；同一 service 同日出现
  多条 `calendar_dates` 记录即失败（同增同删也一样）。
- 仅由 `calendar_dates` 定义的 service（无 `calendar.txt` 行）输出 `weekdays: []`，
  `start`/`end` 取该 service 例外日的最小/最大值，`added` 列出增开日期。
- 引用完整性：`trips.service_id` 必须出现在日历或例外中；`stop_times` 的 trip/stop 必须
  存在；trip 至少两个 call 且 trip 内站点不重复；`query` 的 origin/destination 必须是
  导入的 stop。停运 trip 同样做完整结构与引用校验。
- 换乘时间：GTFS 无此字段，所有站点 `transfer_seconds` 设为 0；即中转缓冲为 0，
  到达即可上下一班。
- 快照数组（stops 按 id、calendars 按 id、trips 按 id、calls 按 stop_sequence）
  均排序，因此 GTFS 行顺序变化不改变输出字节。

失败处理：错误信息形如 `stop_times.txt: record 3 (line 4) [...] : reason`，含文件名、
记录序号（跳过空行后计数）、物理行号与关键 ID；退出码 2。输出写在与目标同目录的临时
文件并原子替换；校验或写入失败时旧输出保持原样，不会留下半成品。

限制：`calendar.txt` 每个 `service_id` 只支持一行；不支持 `frequencies.txt`、
`transfers.txt`、`shapes.txt` 或跨 trip 的贯通车辆语义；导入后按服务日静态查询，
不处理 DST。

## 站点发车供给集市 v1（service-path-supply）

命令 `service-path-supply --source <v1快照> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
--database <SQLite路径>` 把快照展开成自然日发车事实与逐日逐站逐小时汇总。只使用标准库
`sqlite3`，不联网；既有 `service-path`/`service-path-import` 接口与快照字段不变。

输入语义：
- 快照结构与 `timetable.parse` 完全一致（stops/calendars/trips/query），停运 trip 也做
  完整校验；`query` 只校验，不限制输出日期。
- `--start`/`--end` 为闭区间自然日，起点不得晚于终点，区间最多 3660 天。
- 只有 trip 中非末站 call 的 `departure` 计一次发车；末站 call 不发车。
- 服务日候选为 `start-1` 到 `end`，逐日按"常规闭区间 ∧ weekdays，或 added"且不在
  removed 中判断启用；added/removed 可为区间外日期，removed 优先于 added 与常规服务。
- 服务日 `s` 的 `HH:MM:SS`（00..47 小时）换算：自然日 `s + seconds//86400`，
  日内秒 `seconds % 86400`。仅保留自然日落在 `[start, end]` 的发车；因此区间第一天会
  包含前一服务日跨午夜的发车，区间内每个自然日都完整。

表结构（数据库可已存在）：
- `departures(service_day TEXT, trip_id TEXT, call_index INTEGER, natural_day TEXT,
  stop_id TEXT, day_seconds INTEGER CHECK 0..86399)`，主键
  `(service_day, trip_id, call_index)` 保证发车事实唯一；`call_index` 为 0 起的 call 序号。
  附加索引 `(natural_day, stop_id, day_seconds)`。
- `hourly_departures(natural_day TEXT, stop_id TEXT, hour INTEGER CHECK 0..23,
  departure_count INTEGER, max_headway_seconds INTEGER CHECK 1..3600)`，主键
  `(natural_day, stop_id, hour)`。
- 每个快照站点在区间内每个自然日都有 24 行，无服务的要点 `departure_count=0`、
  `max_headway_seconds=3600`。`departure_count` 统计事实行数，同秒多车分别计数。
- `max_headway_seconds` 取该小时内**去重后的发车秒**加上整点 `hour*3600` 与
  `(hour+1)*3600` 两个边界，对排序后相邻点作差取最大值；右边界整点的发车属于下一小时，
  其所在小时用本小时边界计算。只有一班车时等于 3600。

重跑与失败语义：
- 每次运行先完整校验快照，再在临时 SQLite 暂存缓存里流式算好事实与汇总，最后才打开
  目标数据库；坏快照不会接触任何既有数据库。
- 计算为有界内存流水线：事实按固定 1000 行批次写入暂存缓存，汇总时按
  `(natural_day, stop_id, day_seconds)` 顺序把事实流读回来，逐小时维护去重发车秒集合
  （一小时至多 3600 个整数）并与"自然日 × 站点 × 24 小时"网格做单次有序归并，空单元
  写 `(0, 3600)`。除输入快照与 SQLite 缓存外，Python 额外内存不随事实或汇总总行数
  线性增长：常驻结构只有一个批次、单日启用日历集合和单小时秒集合。
- 发布时在单个事务内 `ATTACH` 暂存缓存，删除 `natural_day BETWEEN start AND end` 的
  两表分区，以 `INSERT ... SELECT ... ORDER BY 主键` 直接从缓存复制新事实与汇总，
  提交后再 `DETACH`；该区间以外的行一律不动，行不经过 Python 列表。
- 已有数据库若已有同名表但列/主键结构不符则失败，不修改数据库。
- 校验失败、SQLite 错误或插入失败均回滚事务；运行前不存在的数据库在失败时不留下文件
  （含 -journal/-wal/-shm）。暂存缓存在任何退出路径（含构建中途异常）都会关闭并删除，
  同样不留边车文件。成功退出 0 且在 stdout 打印统计 JSON；失败退出 2，错误写 stderr，
  旧库保持原样。
- 数组顺序（stops/calendars/trips 及 weekdays/added/removed）不改变逻辑结果；输出表按
  主键排序写出。

限制：不处理 DST、实时延误、步行边或跨 trip 贯通车辆；单次区间最多 3660 个自然日、
事实与汇总各最多 500 万行；不对数据库做跨进程并发写入协调，也不承诺文件系统断电恢复。

有界内存与性能复核（`scripts/perf_supply.py`，仅标准库）：
- 同一固定合成快照（10 站、80 个/日的全日历班次）分别跑 100 天与 3000 天两个自然日窗口，
  每个窗口在独立、干净的子进程中用 `tracemalloc` 覆盖建库（build + 写库）全过程。
- 长窗口至少物化 200 万发车事实与 70 万汇总行；脚本打印两窗口的事实行数、汇总行数、
  耗时（秒）与 Python 峰值（MiB），并断言两次峰值差不超过 32 MiB，达不到阈值则退出 1。
- 运行：`python3 scripts/perf_supply.py`（`--days 短 长` 可改窗口，`--keep-db`
  保留发布出的数据库）。
