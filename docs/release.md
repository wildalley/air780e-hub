# 发布清单

这份清单把可自动化的发布步骤固定下来，但不替仓库所有者决定支持承诺。当前包和应用版本均为 `0.1.0`，尚未创建正式 Git tag。

推 `v*` tag 会触发 [`Release` 工作流](../.github/workflows/release.yml)，它把下面「自动检查」和「版本与制品」两节里可自动化的部分全部执行一遍。**本文件保留的手工步骤是它无法验证的那些** —— 真实硬件、真实浏览器、真实恢复演练。

## 发布前决策门槛

首个 Release 前必须明确：

1. ~~许可证~~ —— 已确定：[MIT](../LICENSE)；
2. ~~发行方式~~ —— 已确定：**源码构建 + GHCR 多架构镜像**。源码构建是参考路径，镜像是便利而非替代；只发 `X.Y.Z` 与 `X.Y` tag，不发可变 `latest`；
3. 支持矩阵：Python、Node、Linux 发行版、Air780E 型号和 AT 固件；
4. 版本策略和兼容性承诺。

剩余决定完成后，把结论同步到 `README.md`、`SECURITY.md` 和本文件，再创建版本号对应的 tag。

## 自动检查

在目标提交上确认 GitHub Actions 的 `CI` 和 `Security` 全部通过：

- Agent / Server 的 ruff 检查；
- Agent / Server 在 Python 3.11、3.12、3.13 上的测试；
- 前端 `npm ci`、ESLint、Vitest 与生产构建；
- `docker compose -f deploy/docker-compose.yml config --quiet`；
- 本地 Markdown 链接检查；
- 全 Git 历史 Secret scan；
- Python 与 npm 锁定依赖审计；
- Python 与 JavaScript/TypeScript 的 CodeQL 代码扫描；
- 构建镜像的 HIGH / CRITICAL 漏洞扫描。

再在一台干净的部署主机执行：

```bash
python3 deploy/self_check.py --url https://sms.example.com --token-file /run/secrets/air780e-agent-token
```

输出必须同时包含 health endpoint 和 WebSocket 两项 `PASS`。自检 Token 文件应为
`0600`，检查结束后删除临时副本。

## 版本与制品

推 tag 前，手工完成 1–3：

1. 更新 `CHANGELOG.md`，将本次变更从 `Unreleased` 移到选定版本，并写明日期。**工作流会校验存在 `## [X.Y.Z]` 小节**，缺失即失败 —— 没有条目的版本等于没有文档。
2. 同步 `agent/pyproject.toml`、`server/pyproject.toml`、`frontend/package.json`、`frontend/package-lock.json` 以及两个 Python 包的 `__version__`。**工作流会校验三处版本号与 tag 一致**，对不上即失败。
3. 在干净工作树创建带版本号的提交和 tag；不要把数据库、配置、日志、Token 或前端构建产物加入提交。

推 tag 后，`Release` 工作流自动执行：

- 在 tag 指向的树上复跑 ruff、pytest、ESLint、Vitest 和前端构建 —— master 绿不等于 tag 绿，tag 可能落在别的提交上；
- 构建并推送 `linux/amd64` + `linux/arm64` 镜像到 GHCR，tag 为 `X.Y.Z` 和 `X.Y`；
- 对**已推送的摘要**执行 Trivy HIGH / CRITICAL 扫描（不是同 Dockerfile 的本地重建 —— 扫的必须是真正发出去的字节）；
- 生成 SPDX SBOM 与构建来源证明（attestation），SBOM 附到 Release；
- 按 CHANGELOG 小节起草 Release，正文附镜像摘要。

Release 是**草稿**，需人工确认后发布。发布前在干净主机完成:

4. 在全新目录按 [部署指南](deploy.md) 安装制品，完成一次健康检查、WebSocket 自检、登录和短信收发验收。**按摘要拉取**，不要用 tag：
   ```bash
   docker pull ghcr.io/<owner>/air780e-hub@sha256:<摘要>
   ```
5. 打开运维中心确认磁盘、Agent 和运行时指标正常，制造并恢复一个可控测试事件，确认事件状态与管理审计均按预期更新。
6. 确认镜像与源码构建行为一致 —— 镜像是便利路径，源码是参考路径，两者不应有功能差异。

## 回滚

保留上一版本镜像摘要和数据库备份。升级失败时先停止流量、保留失败日志，再恢复上一个摘要；不要删除 `hub-data` volume。恢复后重新运行部署自检，并确认历史短信和任务数量未变化。

这是不发 `latest` 的原因:回滚要指向一个确定的字节序列。可变 tag 在回滚时无法回答"恢复到哪一版"，而 `X.Y` 会随补丁版本移动 —— 生产环境的 Compose 文件应写摘要。
