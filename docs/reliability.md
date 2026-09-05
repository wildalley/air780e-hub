# 故障注入与恢复基线

这组基线把断网、进程重启、重复事件、USB 重新枚举和自愈限频压缩成确定性的自动化
循环。它不依赖墙上时钟等待,不访问生产数据库,也不打开真实 `/dev/ttyACM*`。

## 运行

先安装锁定的开发依赖。日常 CI 每个场景运行一次;发布前或可靠性改动后用
`--fault-cycles` 扩大重启和重新枚举次数。

Agent:

```bash
cd agent
uv sync --frozen --extra dev
.venv/bin/pytest \
  tests/test_commands.py \
  tests/test_link.py \
  tests/test_agent.py::test_worker_reclaims_the_same_module_after_usb_reenumeration \
  tests/test_worker_recovery.py \
  --fault-cycles 100
```

Server:

```bash
cd server
uv sync --frozen --extra dev
.venv/bin/pytest \
  tests/test_server.py::test_failed_event_application_rolls_back_and_is_replayable \
  tests/test_server.py::test_lost_ack_replay_survives_repeated_server_restarts \
  --fault-cycles 100
```

循环次数控制故障次数,不是运行秒数。失败时 pytest 会指出具体场景;无需为了凑时长让
测试空等数小时。

## 覆盖与验收

| 故障 | 注入方式 | 验收条件 |
| --- | --- | --- |
| 网络在 ACK 前断开 | 事件已发送,连接在 ACK 入队前结束 | Agent SQLite 仍有同一 seq;进程重开后原样重放,收到 ACK 才清空 |
| 大批积压恢复 | 队列写入 `BATCH + 1` 条并逐条 ACK | 跨过 200 条批次边界后立即继续,序号顺序不变,队列归零 |
| 长扫描占用单模块 | 用可控事件挂起扫描,同时补传 `BATCH + 1` 条事件并查询另一设备 | 扫描未结束时 ACK 已清空积压,另一设备已完成查询;同设备命令仍保持先后顺序 |
| 命令队列满载 | 分别触发单设备、全局和执行队列数量上限 | 超限命令明确报告未开始;ACK 与 `resend_from` 正常处理,容量释放后可继续接收 |
| 执行中断线重连 | 长命令开始后断开 socket,随后建立新连接 | 未开始命令不执行,运行中命令不中断也不重复执行;结果通过新连接补传 |
| Agent 停机 | 同设备同时有运行中和等待中的命令,取消链路或停止整个应用 | 命令任务先退出,回执在 SQLite 关闭前持久化;运行中命令标为结果未知 |
| Server 应用中途失败 | 短信行写入后主动抛错 | 消息与 `ingested` 同时回滚,不发 ACK;重放后只保存一份 |
| Server 重启且 ACK 丢失 | 保留数据库、重建整个应用并反复发送同一 seq | `messages` 和 `ingested` 始终各一行,每次重复仍返回 ACK |
| USB 重新枚举 | mock 模块从 `ttyACM0` 消失并以新编号出现 | 旧端口释放,同一 worker 按 IMEI/ICCID 认领新端口并恢复在线 |
| 自愈限频 | 推进可控时钟并重建 worker | 冷却和滚动 24 小时次数跨进程保留,达到上限只报告一次 |
| 通知重试等待 | 首投返回 mock 503,持久化下一次重试时间后推进到期时间 | 后台投递器继续存活,收到唤醒后完成重试;有效 lease 到期前不空转认领 |

通过标准是所有循环零失败,且没有残留未确认事件、重复短信或绕过自愈限频。测试中的
SQLite 均在 pytest 临时目录创建并销毁。

命令并发测试以可控事件模拟长时间等待,没有实际等待 180 秒,也不代表真实运营商扫描的
耗时或吞吐实测。通知重试用例位于 `server/tests/test_notify.py`,日常 Server 全量测试会执行。

## 真实环境边界

自动化覆盖协议、事务、发现和状态机,但不能证明具体主机的 USB 控制器、供电、内核
`cdc_acm`、Air780E 固件或运营商网络表现。上线前仍应按[部署验收](deploy.md#3-验收)
执行一次真实断网补传和模块热插拔;实测组合回填到 P2 支持矩阵。
