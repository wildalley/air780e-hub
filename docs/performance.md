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

趋势接口用 `ts >= <UTC midnight>` 做范围过滤，`date(ts)` 只用于分组，因此 30 天窗口可以使用 `idx_messages_ts`，而不是对整张表逐行计算日期。CSV 通过只读连接和 `fetchmany(500)` 流式读取，下载期间仍允许 WAL 接收新事件。

基线不模拟多个并发浏览器、网络传输时间或同时写入高峰；这些属于后续长稳故障注入和部署环境验证的范围。出现异常时先保存 `--json` 输出，再结合 `EXPLAIN QUERY PLAN` 和数据库大小判断是查询退化、索引缺失还是磁盘因素。
