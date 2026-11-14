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

当前为内存静态快照，不接真实GTFS接口、不做实时延误预测、交通运营或在线导航保证。
