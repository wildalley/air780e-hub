# agent ↔ server 协议

JSON over WebSocket。**连接方向始终是 agent 主动拨出**,Server 不向 Agent 发起入站连接。

本文示例中的域名、Token、设备标识、号码和消息正文均为虚构值。

- 端点:`wss://<域名>/ws`
- 认证:连接时带 `Authorization: Bearer <token>` 头
- 编码:每帧一个 JSON 对象,UTF-8
- 心跳:WebSocket ping/pong,30 秒一次;90 秒无 pong 视为断开

当前线协议版本为 `1`。Agent 在 `hello.protocol_version` 中声明版本；Server 将其与
自身版本比较并在运维中心显示。软件版本不同会创建 warning 事件，协议版本不同会
创建 critical 事件。`0.x` 阶段不支持跨版本混用，部署时仍须同步升级两端。

部署自检可连接 `/ws?self_check=1`,仍需提供相同的 Bearer Token。认证成功后
Server 返回 `{"type":"self_check","ok":true}` 并正常关闭连接,且不会登记临时
Agent 或修改业务数据。该参数只用于连通性检查,正式 Agent 不应携带。

---

## 1. 可靠性模型

**agent 是数据的唯一真相,server 是镜像。**

上行事件带单调递增的 `seq`(agent 本地 SQLite 持久化,重启不重置)。server 逐条发送
累积 `ack`;agent 只有收到 ack 才删除已确认事件,重连后自主从最小未确认 `seq` 开始
重放。

Server 将 `(agent_id, seq)` 幂等标记和该事件产生的业务写入放在同一个 SQLite 事务中。
只有事务提交后才发送 `ack`;应用失败会整体回滚并结束本次连接,因此后续更大的累积 ACK
不会越过失败事件。通知投递和掉线告警等外部副作用在提交后启动,不延长事务持锁时间。

因此:

- **重复必然发生**(ack 丢失时会重放)。server 端按 `(agent_id, seq)` 去重,必须幂等。
- **乱序不会发生**。agent 按 seq 顺序发送,不并发。队列达到上限时允许只裁剪旧
  `status` 采样,所以接收端不得把 seq 有缺口等同于短信丢失。
- 下行命令带 `cmd_id`,agent 执行后回 `cmd_result`。命令**不持久化** —— 断线期间的命令直接丢弃,由 server 在 agent 重连后按需重发。

保号任务是唯一例外:它由 `sync_tasks` 全量下发并**在 agent 本地持久化**,断网期间照常执行(决策 D3)。

---

## 2. 上行(agent → server)

### `hello`

连接后的第一帧。`last_seq` 是 Agent 已分配过的最高序号,用于诊断;待确认队列由
Agent 自主重放,正常连接不依赖 Server 根据该值计算范围。

```json
{
  "type": "hello",
  "agent_id": "site-a",
  "version": "0.1.0",
  "protocol_version": 1,
  "last_seq": 1420,
  "devices": [
    {
      "name": "modem-a",
      "port": "/dev/ttyACM0",
      "online": true,
      "imei": "000000000000001",
      "iccid": "89000000000000000001",
      "model": "AirM2M_780E_V1171_LTE_AT",
      "hardware_model": "Air780EPV",
      "firmware": "AirM2M_780EPV_V1011_LTE_AT",
      "operator": "EXAMPLE",
      "smsc": "+10000000000",
      "registered": true,
      "radio_enabled": true,
      "eps_registered": true,
      "cs_registered": false,
      "ims_registered": false
    }
  ]
}
```

`version` 是发布版本，`protocol_version` 是 JSON 帧契约版本；两者用途不同，均须
上报。旧 Agent 未携带协议字段时按 `0` 处理，因此会明确显示为协议不兼容。

`eps_registered`、`cs_registered` 和 `ims_registered` 都允许 `null`:表示固件没有
暴露该注册域或查询失败,与明确的 `false` 不同。IMS 字段只用于诊断;Server 不因其为
`false` 或 `null` 阻止发送。上述新增设备字段是协议 v1 的可选扩展,旧 Agent 缺失时
Server 和前端按未知状态处理。

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
  "dcs": 0,
  "alphabet": "gsm7",
  "binary": false,
  "pdu": "0891683108200105F0..."
}
```

`ts` 取短信中心时间戳(SCTS);缺失时用 agent 收到的时间,并置 `"ts_source": "local"`。
`binary=true` 表示 8-bit DCS、端口寻址数据、UDH 损坏且正文边界不可信，或无正文的
短信中心专用 PID 控制消息。Server 保留其 PDU 用于诊断，但不会把内容当作可读文本
发送到通知渠道。

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
`refs` 是各分段 `AT+CMGS` 返回的 TP-MR，顺序与短信分段一致。Agent 默认在每个
SMS-SUBMIT 上设置 TP-SRR，请求短信中心返回状态报告。

### `sms_delivery` —— 运营商送达回执

```json
{
  "type": "sms_delivery",
  "seq": 1423,
  "device": "a",
  "iccid": "8986...",
  "reference": 12,
  "peer": "10086",
  "status": "delivered",
  "status_code": 0,
  "service_center_ts": "2026-08-02T18:00:00+08:00",
  "discharge_ts": "2026-08-02T18:00:05+08:00",
  "ts": "2026-08-02T18:00:06+08:00",
  "pdu": "0791..."
}
```

Agent 通过 `AT+CNMI=2,1,0,1,0` 接收 `+CDS`，解码 SMS-STATUS-REPORT 后持久化到
事件队列。`reference` 是 TP-MR，`status_code` 是原始 TP-ST；`0x00–0x1F` 映射为
`delivered`，`0x20–0x3F` 为短信中心仍会重试的 `pending`，`0x40–0x7F` 为
`failed`。Server 以原始 TP-ST 为准，并用短信中心提交时间处理 TP-MR 循环复用。
多分段短信在 Server 聚合为 `pending`、`partial`、`delivered` 或 `failed`。

### `status` —— 设备状态采样

```json
{
  "type": "status",
  "seq": 1423,
  "device": "a",
  "ts": "2026-08-02T18:00:00+08:00",
  "online": true,
  "registered": true,
  "radio_enabled": true,
  "eps_registered": true,
  "cs_registered": false,
  "ims_registered": false,
  "operator": "CHINA MOBILE",
  "rssi": 24,
  "dbm": -65,
  "bars": 5,
  "rsrp": 55,
  "rsrq": 20,
  "storage_used": 0,
  "storage_capacity": 50,
  "voltage_mv": 3968,
  "low_voltage_mv": 3500
}
```

采样周期默认 60 秒。EPS、CS 或 IMS 注册域变化会立即使本次采样值得上报。
**变化不大时不上报**(rssi 变动 < 2、供电变动 < 50 mV 且其余字段不变则跳过),
避免信号曲线被噪声灌满。供电**跨过阈值**不受这 50 mV 限制:那一个样本是告警的边沿,
不能等最长 15 分钟的心跳。

`voltage_mv` 是模块供电电压(`AT+CBC`),取不到时为 `null` —— 固件拒答与旧版 Agent
在 Server 看来一样,都不会去动已有的供电告警,沉默不算恢复。`low_voltage_mv` 是判定
下限,**由 Agent 随帧带上**而不是 Server 自己配:多低算低是这块模块供电方式的属性
(实验室电源与经 hub 的长 USB 线不是一个健康区间),只有 Agent 的 `low_voltage_mv`
配置知道。Server 只负责判定,不保存第二份默认值,免得两边对同一个电压有不同意见。
低于阈值开 `device_supply_voltage` 告警,低于 3300 mV 升为 critical —— 到那儿模块还能
跑,但一次发送突发就可能掉电重启,现象是随机掉网而不像供电问题。

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

设备自愈沿用 `log` 帧，并增加可选结构字段，因此旧 Server 仍可安全保存：

```json
{
  "type": "log",
  "seq": 1427,
  "level": "warning",
  "device": "a",
  "message": "automatic recovery started: operator_reselect",
  "event": "device_recovery",
  "action": "operator_reselect",
  "outcome": "started",
  "reason": "module remained unregistered for 300s",
  "attempt": 1
}
```

`action` 为 `serial_reconnect`、`operator_reselect`、`radio_cycle`、
`module_reset`、`registration_recovery` 或 `registration_watch`；`outcome` 为
`started`、`failed`、`succeeded`、`cancelled` 或 `exhausted`。新 Server 据此打开、
升级和自动解决运维事件。
这些字段是协议 v1 的向后兼容扩展，不改变顶层帧类型。

**来电和保号呼叫也走 `log` 帧**,没有单独的事件类型:

```json
{"type": "log", "seq": 1431, "level": "info", "device": "a",
 "message": "来电 13800138000（未接听，仅记录）"}
```

来电**从不接听**,只记录 —— 部分套餐把接到的来电也算活跃,而这条记录同时回答了"这张卡还能不能被叫到"。
模块的 `RING` 会每隔几秒重复一次,agent 把一通来电内的重复合并成一条,否则一次未接来电会刷出十几行日志。
号码来自紧跟 `RING` 的 `+CLIP`(需 `AT+CLIP=1`),模块不报时记为"未知号码"。

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

`action`:

| 动作 | 用到的字段 | 说明 |
| --- | --- | --- |
| `send_sms` | `target_number`、`content`、`random_suffix` | 默认动作 |
| `ping` | `content`(目标主机,空则 `www.baidu.com`) | 消耗一点流量 |
| `voice_call` | `target_number` | 拨号、响铃数秒后立即挂断 |
| `raw_at` | `content`(AT 指令) | 自定义指令 |

`voice_call` 用于**要求通话记录**的套餐:短信不算活动,但一次呼叫尝试算。它发
`ATD<号码>;`(分号表示语音而非数据呼叫),期间轮询 `AT+CLCC` 看网络把呼叫推进到
哪一步,到 8 秒无论如何 `ATH` 挂断 —— 对端通常是用户自己的另一个号码,而调度可能
落在凌晨,所以刻意不让它被接通。

判定成功的依据是**网络是否真的受理了这次呼叫**,不是"有没有报错":`ATD` 以
`BUSY`、`NO ANSWER` 结束,或 `+CLCC` 报到 `<stat>=3`(对端正在响铃),都说明运营
商记下了一次呼叫尝试,即算成功 —— 而这几个码在 AT 层都是 `ATCommandError`。反过
来,`ATD` 回了 OK 但 `+CLCC` 从头到尾没列出呼叫,或只停在 `<stat>=2` 没推进,都
按失败上报:一张每次呼叫都悄悄失败的卡,不能算保号成功。`+CME ERROR`、`ERROR`、
端口掉线这类"根本没拨出去"的情况直接抛错,由调度按失败重试。

⚠️ 漫游卡上 CS 域往往并不真正可用(`+CREG` 报 home、`+CEREG` 报 roaming 的情况见
`docs/at-reference.md`),此时语音保号会和短信一样发不出去。这个动作按"可能失败"
设计,失败回执里带的是与短信相同的注册状态上下文。

### `run_task` —— 手动执行保号任务

```json
{"type":"run_task","cmd_id":"c-9a03","task_id":3}
```

Agent 校验本地任务后立即启动异步执行，并返回
`{"task_id":3,"status":"started"}`。任务停用只影响定时调度，管理员仍可手动执行；
同一任务已经运行时会拒绝重复启动。最终结果仍通过常规 `task_result` 上报，因此重试、
任务日志和结果通知与定时执行完全一致。

### `query`

```json
{"type": "query", "cmd_id": "c-9a04", "device": "a", "what": "status"}
```

`what`:`status` | `info` | `storage`。

### `set_radio` —— 飞行模式 / 射频开关

```json
{"type": "set_radio", "cmd_id": "c-9a05", "device": "a", "enabled": false}
```

Agent 通过 `AT+CFUN=0/1` 切换并在 `cmd_result.data` 返回当前
`radio_enabled` 与 `registered`。关闭射频不关闭 AT 串口，因此同一连接仍可重新开启。
飞行模式是管理员主动状态，不产生“网络未注册”事件；期间到期的保号任务记为
`skipped`，不消耗重试次数，也不推进 `last_run_at`。

### `scan_operators` —— 扫描可见运营商

```json
{"type":"scan_operators","cmd_id":"c-9a06","device":"a"}
```

Agent 执行官方文档中的 `AT+COPS=?`,在 `cmd_result.data.operators` 返回结构化结果:

```json
{
  "operators": [
    {
      "status": 2,
      "long_name": "EXAMPLE MOBILE",
      "short_name": "EXAMPLE",
      "numeric": "00101",
      "access_technology": 7
    }
  ]
}
```

扫描可能持续数分钟,Server 为该命令使用 210 秒等待时间,不走普通 `raw_at` 的
30 秒路径。同一 MCC/MNC 如果由模块按多个接入制式重复报告,Agent 只返回一项。

### `select_operator` —— 手动 / 自动选择运营商

```json
{"type":"select_operator","cmd_id":"c-9a07","device":"a","numeric":"00101"}
```

`numeric` 只接受 5 或 6 位 MCC/MNC;Agent 下发 `AT+COPS=1,2,"<MCCMNC>"`。传
`null` 时下发 `AT+COPS=0` 恢复自动选择。

选网命令的 `OK` 只代表「请求已受理」,不代表「已切换」:模块是异步重选网的。
因此 Agent 在收到 `OK` 后轮询 CS/EPS 注册域(< 30 秒),等到任一路注册成功才读取
`AT+COPS?`;若在窗口内始终未注册,也如实返回当前搜索中的快照。回执包含 `operator`
(当前 COPS 字段)、更新后的 `device` 状态,以及 `settled` 布尔值——`true` 表示手动
选择期间确实观察到注册(或 `AT+COPS=0` 恢复后重新入网),`false` 表示窗口超时、模块
仍处于搜索中。手动模式期间自动注册自愈不会发送 `AT+COPS=0` 撤销选择。

### `network_diagnostics` —— 只读网络诊断

```json
{"type":"network_diagnostics","cmd_id":"c-9a08","device":"a"}
```

Agent 依次查询 `AT+CCED=0,1`、`AT+CCED=0,2`、`AT+EEMGINFO`、`AT*BANDIND?` 和
`AT^SYSINFO`,不解析固件相关字段,而是分别返回原始行和可选错误。五条查询各有 30 秒
AT 超时,Server 为整组保留 165 秒等待时间。单条命令不支持不会使整个诊断失败:

```json
{
  "diagnostics": {
    "cced": {"lines":["+CCED:LTE current cell: ..."],"error":null},
    "cced_neighbors": {"lines":["+CCED:LTE neighbor cell: ..."],"error":null},
    "eemginfo": {"lines":[],"error":"AT+EEMGINFO: ERROR"},
    "bandind": {"lines":["*BANDIND: 0, 39, 7"],"error":null},
    "sysinfo": {"lines":["^SYSINFO: 2,2,1,17,1,7"],"error":null}
  }
}
```

`cced` 的服务小区行第 3 个字段是 IMSI,与 ICCID 同级敏感 —— 见
[at-reference.md](at-reference.md) §2.2。`AT+CCED` 没有裸执行形式,必须带
`<mode>,<dump>` 参数;V1011 不支持 `AT+EEMGINFO`,保留该小节是为了在实现它的固件上
仍然可用。

### `raw_at` —— Web AT 调试台

```json
{"type": "raw_at", "cmd_id": "c-9a09", "device": "a", "command": "AT+CSQ"}
```

危险操作,server 端必须限制在已认证的管理员会话。

### `resend_from`

```json
{"type": "resend_from", "seq": 1400}
```

要求 Agent 在当前连接内从指定 seq 重放。该帧保留给人工诊断或未来的恢复策略;
正常断线恢复由 Agent 自动重放本地未确认队列,当前 Server 不依赖它完成补传。

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
- `1011` Server 内部应用失败;当前事件未 ACK,Agent 应重连并从本地队列重放
