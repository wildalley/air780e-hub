# air780e-hub

面向 Air780E AT 固件模块的多卡短信中枢。项目由本地 Agent、中心 Server 和 Web 管理界面组成，提供短信收发、通知转发、保号任务、设备监控与数据备份能力。

当前状态：M0–M7 已完成；Agent、Server 和前端均有完整自动化测试。公开发布基线的 CI、依赖/镜像扫描、代码扫描、部署自检和 tag 触发的发布流水线已纳入仓库；许可证为 MIT，发行方式为源码构建 + GHCR 多架构镜像（按摘要部署，不发 `latest`）。支持矩阵与版本策略见 [deploy.md](docs/deploy.md) §0：Python 3.11–3.14，遵循 SemVer 且 `0.x` 阶段不承诺向后兼容，**Agent 与 Server 必须同版本部署**。

> 本仓库中的域名、电话号码、IMEI、ICCID、Token 和消息内容均为示例值，不对应真实账户或设备。运行时配置、数据库、日志和凭据不得提交到版本库。

## 核心能力

### 短信

- 多模块、多 SIM 并行工作，数据按 `sim_id` 隔离
- PDU 模式中文与 Unicode 收发
- 长短信自动分段、合并和入库
- 运营商数据短信隔离、原始 PDU 留存及文本 / 数据筛选
- 出站短信逐分段送达回执与会话内状态更新
- 会话视图、就地回复、未读计数与导航角标
- 按卡、号码和正文全文检索
- 验证码高亮与一键复制
- UTF-8 CSV 全量导出

### 通知

- Bark、Telegram、飞书消息卡片、企业微信、钉钉、Webhook 和 SMTP
- 按 SIM、关键词或正则表达式匹配
- 自定义标题、正文模板和规则优先级
- 同一渠道多规则命中时只推送一次
- 失败重试和脱敏审计日志
- 不访问服务商即可检查最终载荷的规则调试器

### 自动化与运维

- 间隔周期与 cron 保号任务
- 短信、Ping 和自定义 AT 三类动作
- 随机延迟、失败重试、手动立即执行和任务结果通知
- 信号、注册状态、运营商、存储与在线状态监控
- 7 / 30 / 90 天短信趋势和信号历史曲线
- 模块掉线告警、飞行模式 / 射频开关、Web AT 调试台
- 串口假在线探测与未注册分级自愈，带持久化冷却、24 小时限频和运维事件闭环
- SIM 计费方式、套餐、手工余额/币种与低余额阈值管理；余额不足、套餐到期和保号期限分别建立运维事件
- 运维中心集中展示服务诊断、Agent 状态、磁盘占用、事件与管理审计
- Agent / Server 软件版本与协议兼容性检测
- 模块离线、网络未注册、短信发送失败、保号任务失败、通知投递失败及 SIM 低余额/套餐/保号期限事件，可确认、手动解决并在恢复后自动关闭
- 短信、状态采样、日志、审计与已解决事件的分项保留期，SQLite 快照备份与恢复
- Agent Token 支持带有限宽限期的在线轮换
- 可调循环次数的断网重放、进程重启、重复事件、USB 重新枚举与自愈限频故障基线
- 响应式桌面和移动端界面，SIM 信息使用独立卡片管理，顶栏提供项目仓库入口

## 架构

```text
Air780E × N
    │ USB / AT
    ▼
Agent（部署在连接模块的 Linux 主机）
    ├── 串口发现与每模块独立 Worker
    ├── 本地 SQLite 断网缓冲
    └── 本地保号调度器
    │
    │ 主动出站 WSS 连接
    ▼
Server（Docker 或 Python）
    ├── WebSocket 网关
    ├── REST API 与 Web 管理界面
    ├── 中心 SQLite 消息库
    └── 通知规则与推送引擎
    │
    ▼
浏览器 / 推送服务商
```

Agent 主动连接 Server，因此连接模块的网络不需要公网 IP 或端口映射。保号任务在 Agent 本地执行，Server 暂时不可达时仍可按计划运行；短信与事件先写入本地队列，连接恢复后补传。

## 安全模型

系统会处理短信正文和验证码，应按敏感业务系统部署：

- Web 管理端强制使用管理员密码和会话认证，不提供免密模式。
- Agent 使用独立预共享 Token 连接 Server。
- 管理员可在线轮换 Agent Token；宽限期只应用于上一个 Token，并应在期限内更新所有 Agent。
- 公网部署必须使用 HTTPS / WSS，并由可信反向代理传递 WebSocket 升级头。
- Server 数据目录包含短信、会话、Token 和推送渠道凭据，必须限制访问并定期备份。
- Agent 配置包含连接 Token，建议权限设为 `0600`。
- 通知日志不记录短信正文，并会清理服务商错误中的 URL。
- `.local/`、数据库、日志、Cookie、`agent.toml` 和 `config.toml` 已被 `.gitignore` 排除。

公开部署前请阅读 [SECURITY.md](SECURITY.md)。

## 环境要求

### Server

- Docker Engine 24+ 与 Docker Compose v2；或 Python 3.11+
- 可持久化的数据卷
- 公网部署时需要支持 WebSocket 的 HTTPS 反向代理

### Agent

- Linux
- Python 3.11–3.14（部署时用发行版自带的 `python3`；[uv](https://docs.astral.sh/uv/) 仅开发需要）
- Air780E USB 版本，刷入 AT 固件
- 当前用户或服务账户拥有 `/dev/ttyACM*` 访问权限

## 快速开始

### 1. 启动 Server

```bash
git clone https://github.com/wildalley/air780e-hub.git
cd air780e-hub
docker compose -f deploy/docker-compose.yml up -d --build
```

Compose 默认仅把服务发布到 `127.0.0.1:8090`。生产环境应通过 HTTPS 反向代理开放，并启用 `/ws` 的 WebSocket 支持。

首次启动后读取 Agent Token：

```bash
docker exec air780e-hub hub-server token
```

浏览器打开部署地址并设置管理员密码。

上线后运行 `python3 deploy/self_check.py`，可同时验证健康检查、WebSocket 反向代理和 Agent Token；完整命令见[部署指南](docs/deploy.md)。

### 2. 安装 Agent

```bash
cd agent
uv sync
uv run air780e-agent --print-example-config
```

将示例保存到版本库外，例如 `/etc/air780e-agent/config.toml`，然后填写：

- `server.url`：`wss://sms.example.com/ws`
- `server.token`：Server 生成的 Agent Token
- 每个模块的 IMEI 或每张卡的 ICCID

```bash
chmod 600 /etc/air780e-agent/config.toml
uv run air780e-agent --check --config /etc/air780e-agent/config.toml
```

完整的 Docker、反向代理、systemd 与串口权限步骤见 [部署指南](docs/deploy.md)。

## 开发

### Agent

```bash
cd agent
uv sync --extra dev
uv run pytest
```

### Server

```bash
cd server
uv sync --extra dev
uv run pytest
```

### Frontend

```bash
cd frontend
npm ci
npm run build
```

开发服务器会把 `/api`、`/ws` 和 `/healthz` 代理到 `127.0.0.1:8080`。

## 文档

- [部署指南](docs/deploy.md)
- [发布清单](docs/release.md)
- [短信性能基线](docs/performance.md)
- [故障注入与恢复基线](docs/reliability.md)
- [变更日志](CHANGELOG.md)
- [通知渠道与规则](docs/notify.md)
- [Agent / Server 协议](docs/protocol.md)
- [AT 指令与硬件说明](docs/at-reference.md)

## 参考与致谢

项目在需求分析、硬件验证和交互设计阶段参考了以下公开资料与相关项目：

| 项目 / 资料 | 本项目参考的内容 |
|---|---|
| [合宙 Air780E AT 指令文档](https://docs.openluat.com/air780e/at/app/at_command) | AT 指令格式、EC618 模块行为和硬件能力边界 |
| [SimAdmin](https://github.com/3899/SimAdmin) | Web 管理功能覆盖、设备状态展示和交互模式；本项目没有 fork，其 ModemManager / QMI 与单 modem 架构不适合当前的 Agent / Server 多卡模型 |
| [chenxuuu/sms_forwarding](https://github.com/chenxuuu/sms_forwarding) | 短信转发、保号任务和通知配置的产品设计思路 |
| [soxfmr/linux-air780e](https://github.com/soxfmr/linux-air780e) | Linux 下的 Air780E USB 枚举、AT 交互和 PDU 处理经验 |
| [y-guang/air780e-sms-cli](https://github.com/y-guang/air780e-sms-cli) | 轻量命令行短信工具的使用流程 |
| [Ansen/Air780E-SMS](https://github.com/Ansen/Air780E-SMS) | LuatOS 侧短信转发方案的相关实现 |

air780e-hub 是独立实现，不包含上述项目复制的源代码。第三方项目及文档仍分别受其原始许可证和使用条款约束。

## 目录

```text
agent/      本地串口驱动、缓冲队列与保号调度器
server/     WebSocket 网关、REST API、数据与通知引擎
frontend/   React + Vite + MUI 管理界面
deploy/     Docker Compose、systemd 与 udev 示例
docs/       协议、部署、通知和硬件文档
```

## 许可

本项目以 [MIT License](LICENSE) 发布。

上表中被参考的项目各自受其原始许可证约束（SimAdmin 为 GPL-3.0，sms_forwarding、
linux-air780e 和 Air780E-SMS 为 MIT，air780e-sms-cli 未附许可证）。air780e-hub 是
独立实现，不包含来自上述任何项目的源代码 —— agent 与 server 为 Python，SimAdmin
后端为 Rust，两者之间不存在代码复用路径。参考范围限于功能覆盖、交互模式与硬件行为
结论，因此 GPL-3.0 的传染性条款不适用于本仓库。
