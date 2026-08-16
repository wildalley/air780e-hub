# Changelog

本文件记录用户可见的变更，遵循 [SemVer](https://semver.org/lang/zh-CN/)。

`0.x` 阶段不承诺向后兼容：minor 升级可能改动数据库 schema 与协议帧，届时在对应
小节显式标注并给出升级步骤。**Agent 与 Server 必须同版本部署**，跨版本混用不受
支持。详见 [deploy.md](docs/deploy.md) §0。

## [Unreleased]

### Added

- Agent 新增设备自动自愈：串口仍存在但 AT 连续无响应时自动重连，持续未注册时依次执行自动选网、射频重启和模块重启；恢复动作带持久化冷却与滚动 24 小时限频，并在 Server 运维中心建立、升级和自动解决事件。管理员主动关闭射频时不会触发恢复。
- 出站短信逐分段请求运营商送达回执，Agent 持久化上报 `sms_delivery`，Server 按模块、号码、TP-MR 与短信中心时间关联并聚合为“等待回执 / 部分送达 / 已送达 / 投递失败”；打开的会话自动刷新状态。
- Agent `hello` 上报软件版本和协议版本。运维中心显示两端是否一致，版本不同创建 warning 事件，协议不兼容创建 critical 事件，升级一致后自动解决。
- 短信列表、会话和 CSV 导出支持“文本 / 数据”筛选，原始运营商数据短信仍保留用于诊断。

### Fixed

- Agent 与 Server 双重检查 UDH 边界；giffgaff 等运营商发送的损坏 UDH 和空控制短信不再按 GSM-7 渲染成乱码，也不会进入通知渠道，滚动升级期间旧 Agent 的错误标记也会被 Server 纠正。
- 迟到的临时 TP-ST 不再覆盖已保存的终态回执、状态码、时间或原始 PDU。

### Upgrade Notes

- Server 数据库 schema 从 v2 顺序迁移到 v5，启动前自动写入 `hub.db.v<旧版本>.bak` 快照，并重新标记库内已有的数据短信。该版本新增 Agent/Server 协议 v1，部署时必须从同一提交升级并重启 Server 与 Agent；只同步 Agent 源码而不重新执行 `pip install` 仍会运行旧副本。

## [0.1.0] — 2026-08-08

首个公开发布版本。此前所有开发内容合入本版，不单独追溯历史小节。

支持范围：Python 3.11 – 3.14、Node 24（仅构建期）、amd64 / arm64 镜像。实机验证
仅覆盖 Arch Linux + Air780E 固件 `V1011` 一套环境；Debian / Ubuntu 按文档适配但
未实测。完整矩阵见 [deploy.md](docs/deploy.md) §0。

### Added

- 设备页新增按模块控制的飞行模式 / 射频开关。Agent 回读并上报 `AT+CFUN` 状态；主动关闭射频不会触发网络未注册事件，期间保号任务会跳过且不消耗重试次数。
- 飞书通知由纯文本升级为交互式消息卡片，分开展示标题、卡片、发件人、正文和时间；正文使用纯文本节点，短信内容不会被解释为 Markdown。
- 保号任务新增“立即执行”操作；停用任务也可单次手动运行，重试、执行日志和结果通知沿用定时任务链路，并阻止同一任务重复并发。
- 后台界面重新梳理信息层级：仪表盘优先显示待处理事件并在宽屏完整排列设备卡片，设备页使用紧凑列表与详情抽屉，SIM 信息改为响应式独立卡片，通知配置按工作流分栏并提供飞书卡片实时预览，低频系统导航和高级诊断默认折叠。
- 发行方式确定为**源码构建 + GHCR 多架构镜像**。`Release` 工作流由 `v*` tag 触发：在 tag 指向的树上复跑全部质量门、校验 tag 与三处版本号及 CHANGELOG 小节一致、构建并推送 amd64 / arm64 镜像、对已推送摘要执行漏洞扫描、生成 SPDX SBOM 与构建来源证明，最后按 CHANGELOG 起草 Release。只发 `X.Y.Z` 与 `X.Y` tag，不发可变 `latest`。
- Compose 支持 `HUB_IMAGE` 覆盖为已发布镜像摘要；未设置时仍走源码构建，克隆后无需访问 registry 即可部署。
- 运维中心「运行统计」卡：24h / 7d 短信收发量、出站发送失败数、通知成功/失败数、任务成功/失败/跳过数，以及各数据表行数分布（按大小降序）。
- 运维中心空状态行改用 `EmptyRow`（内嵌 `position:sticky; left:0` 包裹）：窄屏横向滚动时标签不再偏移到视口外。

### Fixed

- 修复长页面滚动超过首屏后暴露错误底色、运维折叠面板圆角不一致，以及半透明顶栏模糊并遮住左上角品牌的问题；顶栏现使用实色背景、保留清晰品牌间距，并提供项目仓库快捷入口。
- `successRate` 工具函数：输入存在失败时，用 `Math.floor` 代替 `toFixed(1)` 格式化，防止 99.95%+ 四舍五入后显示为 100% 遮盖实际失败记录。
- `Database.activity_stats()` 中出站短信失败只统计 `direction='out'` 行，防止入站失败行混入使分母出现负值。


- Agent 与 Server 接入 ruff 静态检查，并加入 CI。
- 前端接入 ESLint 与 Vitest，覆盖 API 错误处理、时间戳格式化、验证码识别和 MUI 路由链接约定，并加入 CI。
- Python 与 JavaScript/TypeScript 的 CodeQL 代码扫描工作流。
- Agent、Server 和前端的持续集成检查，覆盖锁定依赖、测试、构建、Compose 配置和本地 Markdown 链接。
- Secret scan、Python/npm 依赖审计和容器镜像漏洞扫描工作流。
- `deploy/self_check.py` 部署自检器，验证关键环境变量、健康检查、TLS、WebSocket 代理和 Agent Token。
- 发布清单与 Dependabot 配置，明确首个 Release 前的决策门槛。
- Vite 升级到 7.x，移除旧开发服务器版本的高危路径穿越公告。
- Docker 构建强制使用 npm 和 uv 锁文件，并排除本地凭据、状态与开发环境。
- 运维中心展示服务诊断、Agent 连接、磁盘使用、运行时队列、事件与管理操作审计。
- 模块离线、网络未注册、短信发送失败、保号任务失败和通知投递失败事件支持确认、手动解决与恢复后自动关闭。
- Agent Token 支持带有限宽限期的在线轮换，并禁止轮换由环境变量托管的 Token。
- 批量设备信号历史接口，减少仪表盘刷新时的重复请求。
- 日志、审计与已解决事件的保留期设置：`HUB_LOG_RETENTION_DAYS`、`HUB_AUDIT_RETENTION_DAYS`、`HUB_INCIDENT_RETENTION_DAYS` 和 `HUB_AUDIT_MAX_ROWS`。

### Fixed

- 恢复数据库备份此前对**任何**合法备份都返回 500：`Database.validate_backup` 声明为 `staticmethod` 却引用 `self._REQUIRED_TABLES`，通过完整性校验的文件走到这一行即抛 `NameError`，而端点只捕获 `ValueError`。格式错误的上传在更早一步就被拒，因此这条路径从未被触发过 —— 也就一直没被发现。现改为 `classmethod`，并补上备份/恢复往返与拒绝非 Hub 数据库的测试。
- SMTP 通知改由独立后台线程执行，避免阻塞 Server 的异步事件循环；发送完成或失败后会可靠地回传结果。
- 短信发送失败事件此前按消息编号建指纹，没有任何恢复路径，每次失败都会留下一条永不关闭的事件并持续占用导航角标；现改为按模块聚合，发送成功即自动关闭。
- 模块离线时，它的未注册和发送失败事件不再滞留 —— 这两种状态在离线期间无法恢复，已由掉线事件表达。
- `agent_logs`、`task_logs`、无关联短信的 `notify_logs`、`audit_events` 和已解决的 `incidents` 此前只增不删，现纳入定期清理；未解决的事件不受保留期影响。
- `deploy/self_check.py` 在输出被重定向到同一管道时，[FAIL] 会出现在先前的 [PASS] 之前，容易误判是哪一项检查失败。

### Security

- WebSocket 自检使用与正式 Agent 相同的 Bearer Token 校验，成功后不登记临时 Agent。
- 管理审计不保留请求正文，并对通知失败详情继续执行敏感 URL 脱敏。
- 审计中间件运行在认证之前，未命中真实路由的请求不再写入审计，避免任何能访问端口的人凭空追加审计行填满磁盘；失败的登录尝试仍会记录。审计表另设行数上限。
- Agent Token 文件以 0600 原子创建，不再有「先写入后 chmod」之间的可读窗口；轮换写入较短 Token 时会截断，不留旧 Token 尾部。

## 发布说明

许可证为 [MIT](LICENSE)，发行方式为源码构建 + GHCR 多架构镜像，支持矩阵与版本策略
见 [deploy.md](docs/deploy.md) §0。

发布由 `v*` tag 触发 [release.yml](.github/workflows/release.yml)。打 tag 前确认
`server/pyproject.toml`、`agent/pyproject.toml`、`frontend/package.json` 三处版本号
与 tag 一致，且本文件存在对应小节 —— 工作流会校验这四项，不一致直接失败。

镜像只发 `X.Y.Z` 与 `X.Y` tag，不发可变 `latest`：回滚需要摘要，而可变 tag 让
「当时部署的是哪一版」事后无法回答。生产环境按摘要部署。
