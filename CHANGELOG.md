# Changelog

本文件记录用户可见的变更。版本号在首个正式 Release 前保持为未发布状态。

## [Unreleased]

### Added

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

正式版本、许可证、镜像发行方式和支持矩阵尚未由仓库所有者确认；在这些决策完成前不要把 `Unreleased` 改名为带承诺的公开版本。
