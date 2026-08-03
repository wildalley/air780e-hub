# 实施计划

> 状态:M0–M6 完成,274 个测试通过。两个模块已接入并实测收发,server 已部署上线。
> 本文档是活文档 —— 决策变更、硬件实测结果都回填到这里。

---

## 1. 目标与范围

### 要做的

1. **多渠道推送** —— 收到短信按规则路由到多个渠道
2. **收发短信** —— 实时接收 + Web 主动发送 + 全量入库
3. **定时保号** —— 自定义号码/内容/周期,支持默认值,随机延迟,防拦截,失败重试
4. **富 Web 界面** —— 参照 SimAdmin 的完整度,部署在服务器 Docker
5. **原生多卡** —— 两个模块两张卡,数据模型层面就区分

### 明确不做的

- eSIM / lpac 管理(EC618 无此能力)
- 频段锁定、小区锁定、APN 下发(需要 QMI,EC618 不支持)
- 把模块当网卡拨号上网(不是本项目目的)
- DDNS、WLAN 管理、OTA(SimAdmin 有,但与本项目无关)
- 多用户 / 权限系统(单管理员足够)

---

## 2. 关键决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | **自建,不 fork SimAdmin** | SimAdmin `modem_manager.rs` 6703 行全绑 ModemManager D-Bus 需整个替换;`sms_listener.rs` 直接订阅 D-Bus 信号需重写;`find_modem_path()` 取 `.next()` 硬编码单 modem,与双卡需求在数据模型层冲突;且其形态是"设备自管理"而非 agent/server 分离。可复用的只有功能设计,不是代码 |
| D2 | **agent → server 出站长连接** | 家宽无公网 IP / CGNAT / 动态 IP;反向做需 DDNS + 端口映射,且把读验证码的接口挂上公网 |
| D3 | **保号调度在 agent 本地执行** | 定时任务错过无法补救,不能依赖链路可用性。server 只负责编辑规则并下发 |
| D4 | **agent 本地持久化 + 断网补传** | server 是镜像与控制面,不是唯一真相。断网期间照收照存照发 |
| D5 | **推送由 server 发出** | 走机房/家宽网络,**不消耗 SIM 卡流量** —— 纯保号卡也能用。这是相比模块跑 LuatOS 自推送的实质优势 |
| D6 | **PDU 模式收发短信** | 文本模式中文和长短信都有问题 |
| D7 | **收到即读即删** | SIM 存储仅 20~50 条,存满会丢新短信。真实历史在自己库里 |
| D8 | ~~udev 按 USB 端口路径绑定设备名~~ → **agent 按 IMEI/ICCID 运行时发现** | 两个同型号模块序列号都是 `000000000001`(实测确认),`by-id` 会撞车。改按端口路径绑虽然可行,但那是拿"插在哪个孔"当身份 —— 换口就要改规则,而 udev 读不到 IMEI(它在 AT 层,不在 USB 描述符里),永远分不清哪个模块是哪个。改由 agent 启动与每次重连时探测 `/dev/ttyACM*` 并按身份认领。udev 仍可用(配置里写 `port` 即跳过发现) |

---

## 3. 技术栈(提议,待确认)

**已确认。**

| 层 | 选型 | 理由 |
|---|---|---|
| agent | **Python 3 + pyserial** | AT 调试迭代快;PDU 编解码自实现(不引三方库,便于处理 EC618 差异);可借鉴 soxfmr/linux-air780e |
| server | **Python + FastAPI + SQLite** | 与 agent 同语言,一套技能维护;SQLite 让 Docker 只需一个 volume |
| 前端 | **React + Vite + MUI** | 与 SimAdmin 同栈,UI 模式可直接对照借鉴 |
| 传输 | **WebSocket + 预共享 token** | 出站、双向、实时;比轮询简单 |
| 部署 | server: Docker Compose(1Panel 导入);agent: systemd | |
| 包管理 | **uv** | 本机已有 |

### 部署形态:1Panel

服务器跑 1Panel,已有域名。因此:

- **容器不自己做 TLS**,只监听内部端口(默认 `8080`),证书与 HTTPS 由 1Panel 的 OpenResty 反代处理
- 反代需**显式开启 WebSocket 升级**(`Upgrade` / `Connection` 头透传),否则 agent 连不上
- agent 侧连 `wss://<域名>/ws`,TLS 在反代终止
- 数据用具名 volume 挂 `/data`(SQLite + 配置),方便 1Panel 备份

**备选**:若日后 agent 要挪到软路由/NAS,Go 编译单静态二进制更省事。届时 agent 可单独用 Go 重写 —— 协议是 JSON over WS,换语言不影响 server。

---

## 4. 架构

```
agent (Arch, 本地)                      server (Docker, 公网)
┌────────────────────────┐              ┌──────────────────────────┐
│ SerialWorker ×N        │              │ WS Gateway               │
│  ├ AT 编解码 / URC 解析 │              │  ├ 认证 (token)          │
│  ├ PDU 编解码          │  ── WSS ──>  │  ├ 事件入库              │
│  └ 命令队列(串行化)     │  <── 下发 ── │  └ 命令下发              │
│                        │              │                          │
│ LocalStore (SQLite)    │              │ REST API (FastAPI)       │
│  └ 未确认事件队列       │              │ 推送引擎 + 规则匹配       │
│                        │              │ SQLite                   │
│ Scheduler(保号)        │              │ 静态前端托管              │
│  └ cron / interval     │              └──────────────────────────┘
└────────────────────────┘
```

**每个模块一个 SerialWorker。** AT 是半双工协议,同一串口上的命令必须串行化(一个命令未收到 `OK`/`ERROR` 前不能发下一条),但不同模块之间完全并行。

**URC 与命令响应要分离。** `+CMTI:` 这类主动上报可能在任意时刻插进命令响应中间,解析器必须能区分。这是 AT 驱动最容易写错的地方。

---

## 5. 数据模型(草案)

```
devices          模块
  id, name, usb_path, symlink, enabled, last_seen_at, fw_version, imei

sims             卡(与模块解耦,便于换卡后保留历史)
  id, iccid, label, phone_number, operator, smsc, device_id, note

messages         短信
  id, sim_id, direction(in|out), peer, body, ts,
  status(received|queued|sent|failed), raw_pdu, parts, error

channels         推送渠道
  id, type(bark|telegram|feishu|wecom|dingtalk|post|get|smtp),
  name, config(json), enabled

rules            转发规则
  id, sim_id(null=全部), match(all|keyword|regex), pattern,
  channel_id, template, priority, enabled

tasks            自动化 / 保号
  id, sim_id, name, action(send_sms|ping|raw_at),
  schedule_type(cron|interval), schedule_expr,
  payload(json: 号码/内容/随机后缀开关/ping目标),
  jitter_seconds, retry_max, enabled, last_run_at, next_run_at

task_logs        任务执行日志
  id, task_id, started_at, status, detail, retry_count

notify_logs      推送日志
  id, message_id, channel_id, status, http_code, response, ts

device_status    状态快照(信号曲线)
  id, device_id, ts, csq, rsrp, rsrq, operator, reg_status, cell_id
```

**关键:所有短信、任务、规则都挂在 `sim_id` 上,不是 `device_id`。** 换卡不丢历史,这是 SimAdmin 单 modem 模型给的教训。

---

## 6. 通信协议(草案,M2 细化到 docs/protocol.md)

JSON over WebSocket。agent 每条上行事件带单调递增 `seq`,server 逐条 ack;agent 重连后重放未 ack 的。

**agent → server**
```
hello        { agent_id, version, devices: [...] }
sms_in       { seq, sim_iccid, peer, body, ts, pdu }
sms_result   { seq, cmd_id, status, error? }
status       { seq, device_id, csq, rsrp, operator, reg, ts }
task_result  { seq, task_id, status, detail, ts }
log          { seq, level, msg }
```

**server → agent**
```
ack          { seq }
send_sms     { cmd_id, sim_iccid, peer, body }
sync_tasks   { tasks: [...] }        # 全量下发,agent 落地后本地调度
query        { cmd_id, device_id, what }
raw_at       { cmd_id, device_id, command }   # Web AT 调试台
```

---

## 7. 里程碑

每个阶段结束都应是**可运行、可验证**的状态。M0–M5 **不需要硬件**。

### M0 — 假模块 + AT 驱动层 ✅ *(完成)*

已实现:

- `pdu/gsm7.py` —— GSM 03.38 默认字母表 + 扩展表,septet 打包/解包(含 UDH 对齐用的 fill bits)
- `pdu/codec.py` —— SMS-DELIVER / SMS-SUBMIT 编解码,GSM7 与 UCS2,长短信分段,字母数字发件人,SCTS 时间戳
- `pdu/concat.py` —— 长短信重组,按 (发件人, ref) 分组,超时兜底输出残缺内容而非丢弃
- `at/client.py` —— 命令串行化、URC 分离、`> ` 提示符、`+CME`/`+CMS` 错误码映射、超时
- `at/transport.py` —— `SerialTransport`(pyserial + `add_reader`)、`FdTransport`、进程内 `PipeTransport`、pty 工具
- `mock.py` —— 假 Air780E,按官方手册格式响应,**含存储容量上限与满时静默丢弃**
- `modem.py` —— 高层驱动:初始化、信息、信号、收件箱、发送、ping、即读即删
- `probe.py` —— 探测工具,同时也是到货后的硬件验证工具

**验收结果:69 个测试通过**,并通过真实 pty + pyserial 端到端验证:中文短信、400 字长短信三段重组、存储清空、UCS2 发送。

开发中发现并修掉的两个真问题(已加回归测试):

1. **`+CEREG:` 既是 URC 又是 `AT+CEREG?` 的响应** —— 注册了 URC 处理器后,查询响应被误路由,导致注册状态永远读不到。修法:命令在途时,以该命令自身的响应前缀优先
2. **URC 前缀互相遮蔽** —— `+CMT` 会吞掉 `+CMTI`,且结果取决于注册顺序。修法:前缀匹配要求后接 `:` / 空格 / 逗号边界

### M1 — agent 骨架 ✅ *(完成)*

- `config.py` —— TOML 配置,server 段留空即为纯本地模式
- `store.py` —— 本地 SQLite:事件队列(`AUTOINCREMENT` 保证 seq 不复用)、短信历史、任务表
- `worker.py` —— 每模块一个 worker,断线指数退避重连(带抖动,避免两个模块同步重试)
- `link.py` —— 出站 WebSocket,累积 ack,断线重放
- `app.py` / `cli.py` —— 编排与入口
- `deploy/systemd/` + `deploy/udev/`

**验收结果:109 个测试通过。** 关键覆盖:两模块并行且 ICCID 分离、长短信只入队一次、事件重启不丢、状态采样去噪(相同样本不重复上报)、**短信正文不进日志**。

### M2 — server 骨架 + 协议 + Docker ✅ *(完成)*

- `docs/protocol.md` —— 协议定稿(幂等、累积 ack、全量 sync_tasks、关闭码)
- `db.py` —— 全量 schema,**一切挂 `sim_id`**;`ingested(agent_id, seq)` 保证重放不重复
- `auth.py` —— scrypt 单管理员密码 + 服务端会话(哈希存储),**无免密开关**
- `gateway.py` —— WS 网关、事件入库、命令下发与 `cmd_result` 关联
- `api.py` —— REST 全量接口(含 M4/M5 的 CRUD)
- `main.py` —— 单进程单端口同时提供 API / WS / 前端
- `Dockerfile` + `deploy/docker-compose.yml` + `docs/deploy.md`

**验收结果:29 个测试通过。** 关键覆盖:重放同一 seq 不产生重复短信、两张卡消息分离、agent 断开后设备置离线、重复 agent_id 被拒(4003)、坏 token 被拒(4001)、匿名访问全部 401。

开发中发现的问题:

3. **会话 Cookie 的 `Secure` 标记不能写死。** 原本按配置项设,结果局域网直连(无 TLS)时浏览器根本不回传 Cookie,永远登不进去。改成按请求实际 scheme 判断 —— 这也是反代必须传 `X-Forwarded-Proto` 的原因
4. **scrypt 参数正好卡在 OpenSSL 默认 32MiB 上限**,必须显式给 `maxmem`

### M3 — Web 界面 v1 ✅ *(完成)*
- 登录认证 + session
- 短信列表 / 会话视图 / 检索
- 主动发送
- 设备与卡状态页
- **验收**:浏览器能看能发

开发中发现的问题:

6. **MUI 的 `DataGrid` 在 SSR/SSG 场景下需要动态 import**,否则 `npm run build` 会报 `ReferenceError: window is not defined`。解决方案:所有 DataGrid 相关组件用 `React.lazy(() => import(...))` + `Suspense` 包裹
7. **前端构建时 `tsc` 会检查 `dist/` 下的旧产物**,如果之前构建产生了类型错误,第二次构建会直接因旧文件的类型错误而失败。`tsconfig.json` 加了 `"skipLibCheck": true` 并建议构建前 `rm -rf dist`

### M4 — 推送引擎 ✅ *(完成)*
- 渠道:Bark / Telegram / 飞书 / 企业微信 / 钉钉 / 自定义 POST / GET / SMTP
- 规则引擎:按卡 + 关键词/正则 → 渠道
- 模板变量、失败重试、转发日志
- Web 端渠道配置 + 测试发送
- **验收**:一条短信按规则同时命中多渠道,日志完整

开发中发现的问题:

8. **服务商 200 不等于成功**:Telegram、飞书、企业微信、钉钉都会在 HTTP 200 的 body 里返回业务错误码。必须逐家判 —— `telegram: ok==true`、`wecom/dingtalk: errcode==0`、`feishu: code==0 或 StatusCode==0`、`bark: code==200`。判失败时把服务商原文抄进 `detail`
9. **Python `ZoneInfo` 在 `python:3.12-slim` 镜像中不可用**,因为基础镜像没有装 `tzdata`。`pyproject.toml` 加了 `tzdata` 依赖,并对 `ZoneInfoNotFoundError` 兜底回 UTC 并 WARN
10. **`notify_logs` 的 `detail` 字段绝不能写短信正文**。PLAN §10 同意,回归测试锁住
11. **闸门:多规则命中同一渠道要只推一次**(取优先级最高规则的模板),否则一个「全部短信→Bark」加「关键词 验证码→Bark」会变成两条推送

### M5 — 保号调度器 ✅ *(完成)*

- `schedule.py` —— cron(5 段,自实现)与 interval(天)两种调度,jitter 带 floor 防止负抖动把时间推到过去
- `scheduler.py` —— 本地 tick 循环、动作 `send_sms` / `ping` / `raw_at`、线性重试、随机后缀、回执
- `store.py` —— 任务全量替换落地,改调度/改 jitter 时丢弃旧的 `next_run_at` 重排
- server:`/api/tasks` CRUD + `/api/task-logs`,`task_result` 入库并回填 `last_run_at` / `next_run_at`
- `notify.py` —— 执行结果按 `notify_on_result` 推送到渠道
- 前端 `pages/Tasks.tsx` —— 任务编辑 + 执行日志

**验收结果:252 个测试通过**(agent 159 + server 93)。关键覆盖:断网时回执入队等重连、agent 停机期间错过的一次只补跑一次、skipped 不重置周期、随机后缀每次不同、坏 cron 表达式只报一次而不是每 tick 都报。

开发中发现的问题:

12. **agent 本地持久化任务,就必须在每次连接时对齐。** 原本只在 server 上编辑任务时下发,断网期间的删除/停用永远不生效 —— 一条已删除的保号任务会在 agent 上一直跑下去。改成 `hello` 之后立即全量下发一次
13. **连接时那次下发不能等 `cmd_result`。** 回执要由同一个收帧循环读取,而循环正卡在等待里 —— 直到命令超时才解开。改成不带 `cmd_id` 的单向帧
14. **任务编辑的 HTTP 请求也不该等 agent 回执。** 一个连着但没响应的 agent 会把 Web 请求拖满 30 秒命令超时。任务的真相在 server 库里,下发是尽力而为 —— 丢了下次连接会再对齐,全量替换让重复下发无副作用

### M6 — 硬件接入 ✅ *(完成)*

两个模块(`AirM2M_780EPV_V1011_LTE_AT`)实测通过:

- 出厂就是 AT 固件,不用刷
- 三个 ACM 口的接口号 `02` / `04` / `06`,`02` 和 `06` 应答 AT,`+CMTI` 走 `02`
- **两个模块序列号都是 `000000000001`** —— D8 的前提在真机上坐实
- 设备名不再靠 udev:agent 按 IMEI 认领,换 USB 口、重新编号都自愈
- 本机没装 ModemManager,抢串口那条风险不存在(`mmcli` 那条因此没测)
- 存储上限 `SM`/`ME` 都是 **10 条**,比预想紧一倍以上
- agent 装成 systemd 服务,配置在 `/etc/air780e-agent/config.toml`(600)

**验收结果:两个模块同时在线,ICCID 分离,真实收发通。** 一条真实短信从模块到飞书推达用时 **2 秒**;`+CEREG: 0,5` 说明两张卡都在漫游(NZ Skinny / UK giffgaff)。

开发中发现的问题:

15. **`+CMGR` 的长度字段从来没被核对过。** 模块在响应头里明确给出 TPDU 字节数,而 `read_stored()` 只取下一行的 PDU —— PDU 短了照单全收,解出半截正文,日志里一个字都没有。真机上四条里出过一条。改成核对不上就重读,重读还短就打 ERROR
16. **拔掉模块 agent 不知道。** 读失败是从 `loop.add_reader` 的回调里 `raise` 的,只到 event loop 的兜底处理器;它想用 `_reader(b"")` 通知客户端,但 `_feed` 第一行 `if not data: return` 把通知丢了;而 `read_signal` / `storage_usage` 都吞 `ATError`。三者叠加,worker 永远停在 online 对着一个不存在的口
17. **`serial.Serial()` 是同步阻塞调用,却跑在事件循环里。** 探测一个不应答的口时会卡住整个 agent,连 WebSocket 心跳都发不出去,链路被服务端以 ping 超时踢掉。改成 `asyncio.to_thread`
18. **`hello` 可能早于端口发现。** 链路和 worker 是并行起来的,`hello` 带出去的端口可能是空的,server 于是一直显示上一次的旧路径。改成状态帧也带 `port`

### M7 — 完善 ⬜
- [x] 仪表盘 + 信号历史曲线
- [x] 短信界面改成会话式(手机短信那样)
- [ ] 模块掉线告警
- [ ] Web AT 调试台
- [ ] 短信 TTL 自动清理
- [ ] 数据备份 / 恢复

---

## 8. 风险与未验证项

| 风险 | 影响 | 应对 |
|---|---|---|
| 出厂是 LuatOS 固件而非 AT 固件 | M6 阻塞 | LuaTools 可互刷,非阻塞性 |
| 3 个 ACM 口哪个是 AT 口未知 | udev 规则写不准 | 到货逐个 `ATI` 探,`bInterfaceNumber` 回填 |
| 两模块序列号相同 | `by-id` 撞车 | 已决策按 USB 端口路径绑(D8) |
| ModemManager 抢串口 | 响应错乱、偶发超时 | mask 或 udev `ID_MM_DEVICE_IGNORE` |
| SIM 存储溢出丢短信 | **丢数据** | 收到即读即删(D7)+ 启动补读 |
| USB 供电不足致模块重启 | 掉线 | 后置口或有源 hub |
| 保号规则因运营商而异 | 保号失败 | 任务动作做成可组合(短信+ping),到货实测两张卡 |
| 主机关机期间不转发 | 延迟 | 靠 SIM 存储 + 开机补读兜底;若 Arch 非常开需重新评估 |
| 长短信合并边界情况 | 内容截断 | M0 单测覆盖;参考 chenxuuu 的 30 秒超时策略 |
| **验证码短信泄露** | **严重** | 见 §10 |

---

## 9. 已确认 / 待定决策

**已确认:**

1. ✅ **服务器 = 1Panel**,Docker 部署,反代 + 证书交给面板
2. ✅ **已有域名**,agent 走 `wss://<域名>/ws`
3. ✅ **要完善的后台** —— 含仪表盘、信号曲线,即 M7 全做。先出主要功能页面
4. ✅ **保号方式 = 发短信**,周期 / 内容 / 目标号码全部可在 Web 界面自定义修改,并提供默认值

**待定(不阻塞开发,到货后填):**

5. 两张卡的角色分工(都纯保号,还是一张常接验证码)—— 只影响界面默认布局
6. 保号的实际号码 / 内容 / 周期取值 —— 按运营商实测确定,代码里先给默认值

### 保号任务的可配置项(据决策 4 定稿)

每条保号任务支持:

| 字段 | 说明 | 默认 |
|---|---|---|
| `sim_id` | 用哪张卡发 | 必填 |
| `target_number` | 目标号码 | `10086`(可改,也可填自己另一个号) |
| `content` | 短信内容 | `1`(可改) |
| `schedule_type` | `interval` / `cron` | `interval` |
| `schedule_expr` | 间隔天数 或 cron 表达式 | `25` 天 |
| `jitter_seconds` | 随机延迟范围,避开整点 | `1800`(±30 分钟) |
| `random_suffix` | 内容尾部附加随机字符,防运营商拦截重复短信 | 开 |
| `retry_max` | 失败重试次数 | `3` |
| `notify_on_result` | 执行结果推送到通知渠道 | 开 |

---

## 10. 安全基线

这套系统经手**全部短信验证码**,按此标准设计:

- server Web 强制认证,**不提供免密模式**(SimAdmin 有免密开关,本项目不做)
- agent ↔ server:WSS + 预共享 token,token 不进代码库
- 短信内容设保留期 TTL,自动清理,不在公网机器上无限堆积
- 前端不做公开分享链接、导出公开 URL 一类功能
- 服务器容器最小权限,不给 host network
- 日志中短信正文脱敏(只留发件人和长度)

---

## 11. 下一步

M0–M5 完成,不需要硬件的部分已经跑通。接下来:

1. **M7 的软件项**(不等硬件):模块掉线告警、Web AT 调试台、短信 TTL 自动清理的界面入口、数据备份 / 恢复
2. **M6 待硬件到货**:按 `docs/at-reference.md` §4 验证清单逐项验证并回填,§9 的 5、6 也在这时定
