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
- 模块离线、网络未注册和通知投递失败事件支持确认、手动解决与恢复后自动关闭。
- Agent Token 支持带有限宽限期的在线轮换，并禁止轮换由环境变量托管的 Token。
- 批量设备信号历史接口，减少仪表盘刷新时的重复请求。

### Security

- WebSocket 自检使用与正式 Agent 相同的 Bearer Token 校验，成功后不登记临时 Agent。
- 管理审计不保留请求正文，并对通知失败详情继续执行敏感 URL 脱敏。

## 发布说明

正式版本、许可证、镜像发行方式和支持矩阵尚未由仓库所有者确认；在这些决策完成前不要把 `Unreleased` 改名为带承诺的公开版本。
