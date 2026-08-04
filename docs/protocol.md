# agent ↔ server 协议

JSON over WebSocket。**连接方向始终是 agent 主动拨出**,Server 不向 Agent 发起入站连接。

本文示例中的域名、Token、设备标识、号码和消息正文均为虚构值。

- 端点:`wss://<域名>/ws`
- 认证:连接时带 `Authorization: Bearer <token>` 头
- 编码:每帧一个 JSON 对象,UTF-8
- 心跳:WebSocket ping/pong,30 秒一次;90 秒无 pong 视为断开

---

## 1. 可靠性模型

**agent 是数据的唯一真相,server 是镜像。**

上行事件带单调递增的 `seq`(agent 本地 SQLite 持久化,重启不重置)。server 逐条 `ack`。agent 只有收到 ack 才把事件标记为已确认;重连后从最小未确认 `seq` 开始重放。

因此:

- **重复必然发生**(ack 丢失时会重放)。server 端按 `(agent_id, seq)` 去重,必须幂等。
- **乱序不会发生**。agent 按 seq 顺序发送,不并发。
- 下行命令带 `cmd_id`,agent 执行后回 `cmd_result`。命令**不持久化** —— 断线期间的命令直接丢弃,由 server 在 agent 重连后按需重发。

保号任务是唯一例外:它由 `sync_tasks` 全量下发并**在 agent 本地持久化**,断网期间照常执行(决策 D3)。

---

## 2. 上行(agent → server)

### `hello`

连接后的第一帧。server 据此决定要重放多少。

```json
{
  "type": "hello",
  "agent_id": "site-a",
  "version": "0.1.0",
  "last_seq": 1420,
  "devices": [
    {
      "name": "modem-a",
      "port": "/dev/ttyACM0",
      "online": true,
      "imei": "000000000000001",
      "iccid": "89000000000000000001",
      "model": "AirM2M_780E_V1171_LTE_AT",
      "operator": "EXAMPLE",
      "smsc": "+10000000000"
    }
  ]
}
```

### `sms_in` —— 收到短信

```json
{
  "type": "sms_in",
  "seq": 1421,
  "device": "modem-a",
  "iccid": "89000000000000000001",
  "peer": "10086",
  "body": "测试消息 123456",
  "ts": "2026-01-02T09:30:00+08:00",
  "segments": 1,
  "pdu": "0891683108200105F0..."
}
```

`ts` 取短信中心时间戳(SCTS);缺失时用 agent 收到的时间,并置 `"ts_source": "local"`。

### `sms_out` —— 发送结果

```json
{
  "type": "sms_out",
  "seq": 1422,
  "device": "a",
  "iccid": "8986...",
  "peer": "10086",
  "body": "CXHF",
  "ts": "2026-08-02T18:00:00+08:00",
  "status": "sent",
  "refs": [12],
  "cmd_id": "c-8f31",
  "error": null
}
```

`status`:`sent` | `failed`。`cmd_id` 在由下行命令触发时带上,保号任务自发的则为 null。

### `status` —— 设备状态采样

```json
{
  "type": "status",
  "seq": 1423,
  "device": "a",
  "ts": "2026-08-02T18:00:00+08:00",
  "online": true,
  "registered": true,
  "operator": "CHINA MOBILE",
  "rssi": 24,
  "dbm": -65,
  "bars": 5,
  "rsrp": 55,
  "rsrq": 20,
  "storage_used": 0,
  "storage_capacity": 50
}
```

采样周期默认 60 秒。**变化不大时不上报**(rssi 变动 < 2 且其余字段不变则跳过),避免信号曲线被噪声灌满。

### `task_result` —— 保号任务执行结果

```json
{
  "type": "task_result",
  "seq": 1424,
  "task_id": 3,
  "device": "a",
  "ts": "2026-08-02T03:17:42+08:00",
  "status": "ok",
  "attempts": 1,
  "detail": "sent to 10086: \"1 f3a9\"",
  "error": null
}
```

`status`:`ok` | `failed` | `skipped`(设备离线等)。

### `cmd_result` —— 下行命令的回执

```json
{
  "type": "cmd_result",
  "seq": 1425,
  "cmd_id": "c-8f31",
  "ok": true,
  "data": {"lines": ["+CSQ: 24,99"]},
  "error": null
}
```

### `log`

```json
{"type": "log", "seq": 1426, "level": "warning", "device": "a",
 "message": "storage 48/50, draining"}
```

短信正文**不出现在日志里**,只记录诊断所需的最少元数据。

---

## 3. 下行(server → agent)

### `ack`

```json
{"type": "ack", "seq": 1425}
```

确认 `seq` 及之前所有事件(累积确认)。

### `send_sms`

```json
{"type": "send_sms", "cmd_id": "c-8f31", "device": "a",
 "number": "10086", "body": "CXHF"}
```

agent 回 `sms_out`(带同一 `cmd_id`),再回 `cmd_result`。

### `sync_tasks` —— 全量下发保号任务

```json
{
  "type": "sync_tasks",
  "tasks": [
    {
      "id": 3,
      "device": "a",
      "enabled": true,
      "name": "移动卡保号",
      "action": "send_sms",
      "target_number": "10086",
      "content": "1",
      "schedule_type": "interval",
      "schedule_expr": "25",
      "jitter_seconds": 1800,
      "random_suffix": true,
      "retry_max": 3,
      "notify_on_result": true
    }
  ]
}
```

**全量语义**:未出现在列表里的任务,agent 本地删除。空列表即清空。

**何时下发:**

1. **agent `hello` 之后立即下发一次**,内容是该 agent 名下的全部任务。保号任务是 agent 本地持久化的(D3),断网期间在 server 上做的增删改它并不知道;不在连接时对齐,一条已删除的任务会在本地一直跑下去
2. server 上任何任务增删改之后,下发给当前所有在线 agent

**不带 `cmd_id`,agent 不回 `cmd_result`。** 连接时那次是在 server 自己的收帧循环里发出的 —— 回执要由同一个循环读取,等它就是死锁。任务的真相在 server 库里,这一帧是尽力而为:丢了,下次连接会再对齐一次,而全量替换让重复下发无副作用。

任务按**归属的 agent** 路由:任务行带 `agent_id` 时按它,否则按 `device` 当前属于哪个 agent。别的 agent 的任务不会下发过来。

`schedule_type`:
- `interval` —— `schedule_expr` 是天数,从上次执行时间起算
- `cron` —— `schedule_expr` 是 5 段 cron 表达式,本地时区

### `query`

```json
{"type": "query", "cmd_id": "c-9a03", "device": "a", "what": "status"}
```

`what`:`status` | `info` | `storage`。

### `raw_at` —— Web AT 调试台

```json
{"type": "raw_at", "cmd_id": "c-9a04", "device": "a", "command": "AT+CSQ"}
```

危险操作,server 端必须限制在已认证的管理员会话。

### `resend_from`

```json
{"type": "resend_from", "seq": 1400}
```

server 发现缺口时要求 agent 从指定 seq 重放。

---

## 4. 连接与重试

| 情形 | agent 行为 |
|---|---|
| 连接失败 / 断开 | 指数退避重连:1s → 2s → 4s → … 上限 60s,带 ±20% 抖动 |
| 认证失败(4001) | 退避到上限并持续重试,同时本地 WARN —— token 配错时不该疯狂打服务器 |
| 收到 `ack` | 删除本地已确认事件 |
| 队列积压 | 本地事件表上限 100000 条,超出时**丢最旧的 `status` 事件**,短信事件永不丢弃 |

关闭码:

- `4001` 认证失败
- `4002` 协议错误(帧不是合法 JSON / 缺 type)
- `4003` agent_id 冲突(同一 id 已有活跃连接)
