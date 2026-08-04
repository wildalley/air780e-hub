# air780e-hub

多卡短信中枢 —— 基于 Air780E 模块的短信转发、保号自动化与 Web 管理系统。

> **当前状态:M0–M7 完成,278 个测试通过。** 两个 Air780E 已接入,真实收发与推送打通(端到端约 2 秒),server 已部署上线。
> 计划见 [`docs/PLAN.md`](docs/PLAN.md),协议见 [`docs/protocol.md`](docs/protocol.md),
> 部署见 [`docs/deploy.md`](docs/deploy.md),AT 与硬件笔记见 [`docs/at-reference.md`](docs/at-reference.md)。

---

## 这是什么

把插在本地 Linux 机器上的多个 Air780E 4G 模块,变成一套可以从公网 Web 界面统一查看和操作的短信系统:

- 收到的短信实时转发到多个推送渠道(Bark / Telegram / 飞书 / 企业微信 / 钉钉 / Webhook / 邮件)
- 从 Web 界面主动发短信
- 定时自动发短信 / Ping 保号,支持自定义号码、内容、周期
- 查看信号、运营商、注册状态、小区信息
- 短信全量入库,不受 SIM 卡存储上限限制

## 为什么不直接用现成的

调研过三个方向,都对不上:

| 项目 | 为什么不行 |
|---|---|
| [SimAdmin](https://github.com/3899/SimAdmin) | 依赖 ModemManager + QMI,目标是刷了 Debian 的高通 CPE。且 `find_modem_path()` 取 `.next()` —— **硬编码单 modem**,两张卡的场景在数据模型层面就不成立。形态也是"设备自管理",与 agent/server 分离不同 |
| [Ansen/Air780E-SMS](https://github.com/Ansen/Air780E-SMS) | 112 行 Lua,只支持 Bark,无定时保号,2023 年后停更 |
| [chenxuuu/sms_forwarding](https://github.com/chenxuuu/sms_forwarding) | 功能设计很好(保号是主打),但 master 已换成 ESP32C3 + ML307R-DC 方案,硬件对不上 |

**结论:抄 SimAdmin 和 chenxuuu 的功能设计,不 fork 它们的代码。**

参考实现(Linux 侧 AT 驱动可直接借鉴):
- [soxfmr/linux-air780e](https://github.com/soxfmr/linux-air780e) —— Python,PDU 解码、systemd 模板单元、双设备实例
- [y-guang/air780e-sms-cli](https://github.com/y-guang/air780e-sms-cli) —— 更轻的 CLI 骨架

## 架构

```
┌─────────────────────────── 家里 ───────────────────────────┐
│                                                            │
│   Air780E #A ──USB──┐                                      │
│                     ├──> Arch Linux                        │
│   Air780E #B ──USB──┘     ┌──────────────────────────┐     │
│                           │  agent                   │     │
│                           │  · AT 驱动(每模块一 worker)│    │
│                           │  · 本地 SQLite(断网缓冲)  │    │
│                           │  · 保号调度器(本地执行)   │     │
│                           └────────────┬─────────────┘     │
└────────────────────────────────────────┼───────────────────┘
                                         │
                      主动出站 WSS 长连接 │ (家宽无需公网 IP)
                                         ▼
┌─────────────────────────── 服务器 (Docker) ────────────────┐
│   ┌──────────────────────────────────────────────────┐     │
│   │  server                                          │     │
│   │  · WebSocket 网关     · 短信库 (SQLite/PG)        │     │
│   │  · REST API           · 推送渠道 + 规则引擎        │     │
│   │  · 静态前端托管        · 任务/通知日志             │     │
│   └──────────────────────────────────────────────────┘     │
│                          ▲                                 │
│                          │ HTTPS                           │
└──────────────────────────┼─────────────────────────────────┘
                           ▼
                    浏览器 / 手机
```

**关键设计:连接方向是 agent → server(出站)。** 家宽大概率无公网 IP 甚至 CGNAT,反过来做需要 DDNS + 端口映射,还等于把一个能读全部验证码的接口挂上公网。

**关键设计:保号任务在本地执行。** 定时任务错过就是错过,不能赌链路当时是通的。服务器只负责编辑规则并下发。

## 硬件

- 2× Air780E(**USB 版本**,EC618 芯片)
- 必须刷 **AT 固件**(`ATI` 回 `AirM2M_780E_V1171_LTE_AT` 之类);出厂如果是 LuatOS 固件,用 LuaTools 互刷
- 插上后内核 `cdc_acm` 直接认,枚举出 3 个 `/dev/ttyACM*`,其中一个是 AT 口
- 4G 天线必接

细节见 [`docs/at-reference.md`](docs/at-reference.md)。

## 这套方案的一个额外好处

模块只做 4G 信令层的短信收发,**推送走服务器/家宽的网络,不吃 SIM 卡流量**。

所以纯保号卡(零流量或流量按量计费)完全适用 —— 这是相比"模块跑 LuatOS 自己 HTTP 推送"方案的实质优势。顺带也绕开了 LuatOS 不支持 TLSv1.3、http 库要塞进 task、Lua 内存吃紧等一堆限制。

## 功能清单

### 短信
- [x] 实时接收(`+CMTI` URC 驱动)
- [x] 长短信自动合并
- [x] 中文 / Unicode(PDU 模式)
- [x] 主动发送(Web / API)
- [x] 全量入库,自动清空模块存储防溢出
- [x] 按卡、按号码、按关键词检索
- [x] 会话视图(手机短信那样的气泡对话,可就地回复)
- [x] 会话未读计数与导航角标,打开会话自动标为已读
- [x] 验证码自动高亮 / 一键复制,全文搜索与 UTF-8 CSV 全量导出

### 推送
- [x] 多渠道:Bark / Telegram / 飞书 / 企业微信 / 钉钉 / 自定义 POST / GET / 邮件
- [x] 多通道同时启用
- [x] 转发规则:按卡 + 关键词 / 正则匹配路由到不同渠道
- [x] 自定义模板(`{sender}` `{message}` `{timestamp}` `{card}`)
- [x] 推送失败重试 + 转发日志
- [x] 规则调试器:不实际发送即可查看命中规则和最终渲染内容

### 保号自动化
- [x] 定点定时(cron)与间隔周期(每 N 天)两种调度
- [x] 动作:发短信 / `AT+CIPPING` 消耗流量 / 自定义 AT
- [x] 自定义目标号码与内容,支持设默认值
- [x] 随机延迟(避开整点)
- [x] 内容自动附加随机字符(防运营商拦截重复短信)
- [x] 失败自动重试 + 结果推送通知
- [x] 执行日志

### 设备与状态
- [x] 多模块并行,每模块独立 worker
- [x] **按 IMEI / ICCID 自动认领串口** —— 换 USB 口、`ttyACM` 重新编号都不用改配置
- [x] 信号(`AT+CSQ` / `AT+CESQ` RSRP/RSRQ)、运营商、注册状态、小区信息
- [x] 信号历史曲线
- [x] 仪表盘短信量趋势(7 / 30 / 90 天)
- [x] SIM 信息:ICCID、卡状态、短信中心号
- [x] 模块在线/掉线告警
- [x] Web 端 AT 命令行(调试用)

### 系统
- [x] 管理员密码认证 + session
- [x] 断网缓冲与补传
- [x] 短信保留期 TTL 自动清理
- [x] 数据备份/恢复

## 前端

`frontend/` 是 React + Vite + MUI 6 的单页应用,开发时由 Vite 代理 API 到后端,生产构建产物放在
`server/src/hub_server/www`(`npm run build` 后拷入),由 server 在根路径托管,客户端路由由 catch-all 兜底。

设计上做了整层的系统化定制,而不是默认 MUI 风格:

- **设计 token 集中在 `frontend/src/tokens.ts`** —— 深浅两套独立选定(非简单反转)的色板,两套都过了 CVD 无障碍校验(系列色 ΔE ≥ 8);状态色永远配图标 + 文字,不只靠颜色传达
- **数字一律 `tabular-nums`**(信号、短信数、存储),让列对齐
- **排版**:标题负字距 + 紧凑行高(Apple 式),正文接近 0 字距
- **动效克制且可感知**:卡片入场有阶梯式上浮,按钮/图标有按下反馈(`:active` 缩放);全部尊重 `prefers-reduced-motion`,开启后收敛为瞬态
- **全局键盘焦点环**(`:focus-visible`)、半透明毛玻璃顶栏、分组侧边导航、统一 `PageHeader`
- **桌面与移动端共用响应式布局** —— 页面本身不产生横向溢出;宽表在自己的容器内滚动,短信会话在窄屏切换为列表 / 对话单栏
- 图表用 Recharts,多模块信号曲线共用一条 dBm 轴,颜色按模块固定位置取,不随过滤重排

改样式前先读 `tokens.ts` 的注释 —— 里面对每个色值的选定理由和校验结果都有交代。

## 目录结构

```
air780e-hub/
├── README.md
├── docs/
│   ├── PLAN.md                      # 实施计划、里程碑、待定决策
│   ├── at-reference.md              # AT 指令表、硬件坑、到货验证清单
│   └── protocol.md                  # agent ↔ server 协议(M2)
├── agent/                           # 本地侧:串口 + 保号调度      [M0/M1/M5]
│   ├── pyproject.toml
│   ├── src/air780e_agent/
│   │   ├── at/                      # AT 传输、命令串行化、URC 分离
│   │   ├── pdu/                     # PDU 编解码、GSM7、长短信重组
│   │   ├── modem.py                 # 高层驱动
│   │   ├── mock.py                  # 假 Air780E(硬件到货前开发用)
│   │   └── probe.py                 # 探测/验证工具
│   └── tests/
├── server/                          # 服务器侧:API + 推送 + 规则引擎     [M2+M4]
├── frontend/                        # Web 界面                        [M3]
└── deploy/
    ├── docker-compose.yml
    └── systemd/
```

## 开发不用等硬件

`mock.py` 是一个按官方 AT 手册行为实现的假 Air780E,连存储容量上限和满时静默丢消息都照做了(那是本项目最大的丢数据风险)。整套逻辑都能先跑通,硬件到了只是把设备路径从 pty 换成 `/dev/ttyACM*`。

```bash
cd agent
uv sync --extra dev
uv run pytest                        # 159 个测试

# 起一个假模块,它会打印出 pty 路径
uv run python -m air780e_agent.mock
#   > sms 10086 验证码 123456      注入一条短信
#   > fill 45                      灌满存储,看丢消息行为
#   > signal 12                    改信号强度

# 另开一个终端,连上去看
uv run python -m air780e_agent.probe /dev/pts/N --listen
```

`probe` 同时也是**到货后的硬件验证工具**:

```bash
uv run python -m air780e_agent.probe --scan              # 找出哪个 ACM 口是 AT 口
uv run python -m air780e_agent.probe /dev/ttyACM3        # 打印型号/ICCID/信号/存储
uv run python -m air780e_agent.probe /dev/ttyACM3 --send 10086 CXHF
```

## 安全提醒

这套系统会经手你**全部的短信验证码**。设计上必须:

- 服务器 Web 强制认证,不留免密模式
- agent ↔ server 用 TLS + 预共享 token
- 短信设保留期自动清理,不在公网机器上永久堆积
- 前端不做公开分享链接一类的功能

## 许可

未定(自用项目)。注意 SimAdmin 是 GPLv3 —— 本项目**只参考功能设计,不复制其代码**,因此不受其传染。
