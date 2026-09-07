# 短信性能基线

这份基线覆盖管理后台最常用的短信读路径：列表页（含总数）、正文搜索、单个会话、会话聚合、30/365 天趋势，以及全量流式 CSV。它用于发现查询退化，不是对所有硬件都适用的 SLA。

## 运行

在 Server 的开发环境中执行：

```bash
cd server
uv run --frozen python benchmarks/messages.py
```

脚本每次在临时目录创建数据库，不会读取或修改生产库。`--json` 输出可归档的 JSON；`--rows`、`--repeat` 和 `--seed` 可以调整数据量、重复次数和固定随机种子。`--enforce` 才会把验收阈值转成非零退出码，且只接受默认的 100,000 行：

```bash
uv run --frozen python benchmarks/messages.py --json > /tmp/messages-benchmark.json
uv run --frozen python benchmarks/messages.py --enforce
```

## 数据集

- 100,000 条短信、12 张 SIM，覆盖约 347 天；时间锚点和随机种子固定，结果可重复;
- 5% 的记录集中在一个长会话，其余记录分布在 6,000 个卡号/号码组合;
- 同时包含收发方向、未读记录、数据短信、长正文和需要 CSV 转义的换行;
- 数据库使用与 Server 相同的 schema、WAL 和索引，生成阶段结束后重新打开再计时;
- 每个指标先预热一次，再取多次运行的中位数。CSV 指标额外报告 Python 堆峰值；SQLite 自身的页缓存不计入该值。

## 参考结果

以下是 2026-08-16 在 AMD Ryzen 7 5800H、Python 3.14.7、SQLite 3.53.4、Linux x86_64 上的 `--repeat 5` 记录。不同 CPU、磁盘和 Python 版本应以本机重新运行的 JSON 为准。

| 路径 | 中位数 | 默认阈值 |
| --- | ---: | ---: |
| 列表第一页 + 总数 | 约 1.6 ms | 75 ms |
| 正文搜索 + 总数 | 约 40 ms | 300 ms |
| 单会话读取 + 总数 | 约 0.6 ms | 75 ms |
| 会话聚合（前 200 个） | 约 29 ms | 300 ms |
| 30 天趋势 | 约 7 ms | 150 ms |
| 365 天趋势 | 约 88 ms | 750 ms |
| 流式 CSV（100,000 行） | 约 0.9 s（11 万行/秒） | 不低于 15,000 行/秒 |

流式 CSV 的 Python 堆峰值约 1 MiB，脚本阈值为 32 MiB。阈值留有余量，适合在发布前或升级后手动执行；普通 CI 不因共享 runner 的瞬时负载设置绝对延迟断言。

## 实现约束

schema v7 增加 `idx_messages_conversation (sim_id, peer, ts DESC, id DESC)`。会话查询先在该覆盖索引上完成分组、计数和最新时间，再只为限制以内的会话读取预览和未读数；这样不会为每条历史短信取正文。旧数据库启动时自动执行 v6 -> v7 迁移，并在迁移前保留 `hub.db.v6.bak` 快照。

趋势接口使用配置时区的本地日起止转换成半开 UTC 区间，仍通过 `idx_messages_ts` 过滤范围。
自 2026-09-06 起，IANA 时区日桶在只读游标遍历时由 Python 计算，避免 SQLite UTC 分组和固定
offset 无法处理 DST 的问题。CSV 通过只读连接和 `fetchmany(500)` 流式读取，下载期间仍允许 WAL 接收新事件。

基线不模拟多个并发浏览器、网络传输时间或同时写入高峰；这些属于后续长稳故障注入和部署环境验证的范围。出现异常时先保存 `--json` 输出，再结合 `EXPLAIN QUERY PLAN` 和数据库大小判断是查询退化、索引缺失还是磁盘因素。

## 数据库并发调度验收（2026-09-05）

`benchmarks/concurrency.py` 在临时目录生成合成数据，同时运行查询、网关事件入库、CSV
导出和保留清理。两种模式使用相同 SQL、schema 与数据：对照模式恢复共享读锁、事件循环
直接执行 DB 操作与单次状态清理，当前模式使用 WAL 只读连接、有界单线程执行器和分批
清理。它用于比较调度方式，不代表两个发布版本的完整性能差异。

```bash
cd server
.venv/bin/python benchmarks/concurrency.py --output /tmp/air780e-concurrency.json
```

默认每种模式运行 3 轮，交替执行顺序；100,000 条短信、4 个并发读取任务，每个执行
8 次查询，200 条状态事件、一次全量 CSV、清理 1,200 条旧状态。可通过 `--rows`、
`--readers`、`--iterations`、`--events` 和 `--repeat` 调整。脚本检查全部事件已 ACK、
去重计数与清理计数，输出环境和每轮分位数，不在共享机器上强制绝对延迟阈值。

本次环境：Linux x86_64、Python 3.13.14、SQLite 3.53.1、16 个逻辑 CPU。
[原始 JSON](benchmarks/2026-09-05-concurrency.json)记录各轮结果。下表取**每轮指标的中位数**，
并非合并全部样本重新计算的分位数：

| 指标 | 共享锁同步对照 | 当前调度 |
| --- | ---: | ---: |
| 事件循环延迟 p95 | 153.145 ms | 4.517 ms |
| 单轮最大 ACK 延迟 | 272.012 ms | 6.831 ms |
| ACK 延迟 p95 | 0.653 ms | 2.574 ms |
| 共享连接锁等待 p95 | 146.274 ms | 0.009 ms |
| 共享连接锁持有 p95 | 58.901 ms | 1.565 ms |
| 混合查询操作耗时 p95 | 257.610 ms | 160.083 ms |
| 全量 CSV | 1,428.136 ms | 1,659.288 ms |
| 保留清理总耗时 | 1.244 ms | 4.626 ms |

ACK 从进入 `_ingest` 起计时，到模拟 Socket 收到 ACK 为止，不包含事件循环阻塞导致的
接收前延迟。事件在每次 ACK 后等待 5 ms 再发送，对照模式中多数事件发生在长读结束后，
所以必须同时查看 loop lag 与最大 ACK，不能只看 ACK p95。查询耗时包含锁等待和结果
构造；共享锁统计只计算最外层持锁，不重复计入事务内的重入。单线程切换和清理让出队列
增加少量调度成本，多个只读连接也增加 CPU/磁盘竞争，CSV 在此组合负载下有所变慢。

独立运行原有 `messages.py --repeat 3 --json --enforce` 仍全部通过：列表＋总数 4.138 ms、
搜索＋总数 56.279 ms、单会话 1.802 ms、会话聚合 33.036 ms、30/365 天趋势
8.508/88.293 ms；CSV 约 96,915 行/秒，Python 堆峰值 1.037 MiB。

本次不包含 HTTP 认证与 JSON 编码、通知服务商、真实 WSS/串口、20 浏览器、慢磁盘或
持续高负载；不能据此宣称普通 API p95 或生产容量已达到总览中的预算。

## IANA 日桶回归（2026-09-06）

使用相同 100k 合成数据运行 `benchmarks/messages.py --repeat 3 --json --enforce`，
[原始 JSON](benchmarks/2026-09-06-calendar.json)中的全部预算通过。30/365 天趋势中位数为
20.563/224.928 ms，阈值仍为 150/750 ms；查询计划确认 UTC 范围使用 `idx_messages_ts`。
该基准继续采用默认 UTC 日桶以保持固定数据集口径；API 的上海边界和纽约 DST 由独立用例验证。

逐行 IANA 转换比原先 SQLite `date(ts)` 分组需要更多 Python CPU。本轮优先保证时间正确性，
没有放宽性能阈值，也没有声明查询更快。CSV 约 87,377 行/秒、Python 堆峰值 1.037 MiB；
这些独立读路径数字不代表 HTTP p95 或生产组合负载容量。

## 运行指标口径（2026-09-06）

管理员可通过 `/api/operations/diagnostics` 的 `runtime.metrics` 读取进程指标，响应禁止缓存。
指标不持久化；Server 重启清零，在线恢复保留已有进程历史，loop lag 采样随维护暂停和恢复。
`count`、`total_ms`、`max_ms` 为进程累计，`p50_ms/p95_ms/p99_ms` 为最近至多 256 个样本的
nearest-rank 分位数。`sample_count` 表示实际样本数，无样本的分位数与最大值为 null。
这些分位数不是固定时间窗，也不能合并多个进程的分位数得到总体结果。

| 字段 | 测量边界 |
| --- | --- |
| `http[]` | `/api/*`、health、ready 从进入中间件到最后一段 ASGI 响应发送完成；按路由模板、方法、状态码和 outcome 分组，不含响应结束后的审计写入 |
| `http[].outcome` | `complete` 表示响应已完整发送（含 4xx/5xx）；`error/cancelled/incomplete` 表示未完整发送，status 为已发送的状态，未发送响应头时用 500 占位 |
| `loop_lag` | 每秒定时任务的实际唤醒时间减预期时间，使用单调时钟；不是浏览器或代理延迟 |
| `db_queue_wait` | 调用 `Database.run` 至 worker 开始，含准入上限及执行器等待；尚未提交即取消的调用不计样本 |
| `db_worker` | worker 中完整回调耗时，可能含多条 SQL、Python 处理或恢复文件 I/O |
| `db_lock_wait / db_lock_hold` | 最外层共享连接锁的等待/持有时间，不重复统计事务内重入；持有时间含 Python 业务处理 |
| `db_read_session` | 只读连接建立到关闭，含取数、Python 聚合及迭代器消费者的等待；可用于发现长快照，不能当成单条 SQL 执行时间 |
| `event_transaction` | 取得 Python 锁后，从 BEGIN 尝试到 COMMIT 或失败回滚；包含 SQLite 写锁等待，不含执行器及 Python 锁排队 |
| `gateway_ack` | 进入 `_ingest` 至 ACK 成功发送，含 DB 等待和提交后回调；不含接收前排队、网络传输及 Agent 实际接收 ACK 的时间 |

`counters` 中的 `events_committed/events_duplicate/events_failed` 分别表示新事件提交、重复
事件确认和事务失败次数。`ack_send_failed` 单独记录提交后发送 ACK 的异常，成功 ACK 才进入
耗时样本。`db_busy` 按 SQLite 错误码统计 BUSY/LOCKED（含扩展码），不把所有数据库错误都
当成锁冲突。每个 HTTP 系列和固定耗时项最多保存 256 个数值，HTTP 系列最多 256 组，之后
的新组合统一进入 `http_overflow`；已有系列继续累计。只保存框架路由模板及固定类别，不保存
真实路径参数、查询参数、SQL、正文、凭据或异常文本。

`benchmarks/concurrency.py` 保留真实数据库锁和采集器，在外层添加基准计时，并随报告导出
`runtime_metrics`。每轮同时校验实际 ACK、去重表、新提交计数和 ACK 样本数一致。

## 第二批运行指标（2026-09-07）

通知和网关的运行观测已接入同一份 `runtime.metrics`：

| 字段 | 测量边界 |
| --- | --- |
| `timings.notify_send` | 每一次 provider 调用从进入渠道 sender 到返回或抛错；包含网络、TLS、provider 响应解析，不包含渠道查询、队列/信号量等待和 DB 结算 |
| `counters.notify_attempts` | 已进入 provider sender 的调用次数；停用渠道、来源记录不存在等未发起调用的行不计入 |
| `counters.notify_succeeded` / `notify_failed` | provider 调用正常返回或抛出普通异常的次数；HTTP 错误和 provider 在 200 响应中返回的业务错误均计入失败 |
| `counters.notify_cancelled` | provider 调用被协程取消的次数；取消不会被伪记为成功或普通失败 |
| `counters.notify_retry_scheduled` | 持久化投递或一次性投递安排下一次尝试的次数 |
| `gateway_closes` | Server 发起或观察到的 Agent WebSocket 关闭，按固定类别、规范化 code 和次数聚合；不含 close reason、Agent ID 或对端任意文本 |

`gateway_closes` 最多保留 32 组，未列入白名单的类别归入 `other`，不在 1000–4999 范围的
code 返回为 `null`；超出的组合只增加 `gateway_close_overflow`。当前类别包括认证失败、维护、
自检、协议错误、重复 Agent、内部错误和对端断开。指标依旧只存在进程内，响应为 `no-store`，
不会因为这些计数写入业务数据库，也不改变通知至少一次和提交后 ACK 语义。

回归覆盖 `tests/test_metrics.py`、`tests/test_notify.py` 和实际 WebSocket 握手关闭路径；通知
重试场景验证 3 次 provider 调用得到 2 次失败、1 次成功和 2 次重试计数。该验证使用 mock
provider，不能代表第三方服务商延迟或送达率。
