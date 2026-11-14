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
