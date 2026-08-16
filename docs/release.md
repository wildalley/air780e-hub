# 发布清单

这份清单把可自动化的发布步骤固定下来。当前包和应用版本均为 `0.1.0`，尚未创建正式 Git tag。

推 `v*` tag 会触发 [`Release` 工作流](../.github/workflows/release.yml)，它把下面「自动检查」和「版本与制品」两节里可自动化的部分全部执行一遍。**本文件保留的手工步骤是它无法验证的那些** —— 真实硬件、真实浏览器、真实恢复演练。

## 发布前决策门槛

四项均已确定（2026-08-08）：

1. **许可证** —— [MIT](../LICENSE)；
2. **发行方式** —— **源码构建 + GHCR 多架构镜像**。源码构建是参考路径，镜像是便利而非替代；只发 `X.Y.Z` 与 `X.Y` tag，不发可变 `latest`；
3. **支持矩阵** —— 见 [deploy.md](deploy.md) §0。Python 3.11–3.14（CI 全覆盖）、Node 24 仅构建期、amd64 / arm64 镜像；实机验证只覆盖 Arch Linux + 固件 `V1011` 一套环境，Debian / Ubuntu 与 arm64 均未实测；
4. **版本策略** —— 从 `0.1.0` 起遵循 SemVer，`0.x` 阶段不承诺向后兼容，**Agent 与 Server 必须同版本部署**。截至 2026-08-16，schema 顺序迁移、协议版本协商和 Agent 版本上报三个技术前提已经完成；转入 `1.0.0` 仍要求支持矩阵至少实测两个发行版，并完成一个真实保号周期的持续运行观察。

结论已同步到 `README.md`、`CHANGELOG.md` 和 [deploy.md](deploy.md) §0。后续变更支持范围时，这四处要一并更新。

## 自动检查

在目标提交上确认 GitHub Actions 的 `CI` 和 `Security` 全部通过：

- Agent / Server 的 ruff 检查；
- Agent / Server 在 Python 3.11、3.12、3.13、3.14 上的测试；
- 前端 `npm ci`、ESLint、Vitest 与生产构建；
- `docker compose -f deploy/docker-compose.yml config --quiet`；
- 本地 Markdown 链接检查；
- 全 Git 历史 Secret scan；
- Python 与 npm 锁定依赖审计；
- Python 与 JavaScript/TypeScript 的 CodeQL 代码扫描；
- 构建镜像的 HIGH / CRITICAL 漏洞扫描。

涉及消息查询、SQLite 或 CSV 导出的版本，在固定性能主机额外执行：

```bash
cd server
uv run --frozen python benchmarks/messages.py --enforce --json
```

保存输出用于和上一版本比较。绝对延迟不放进共享 CI runner；数据集、阈值和参考结果见[短信性能基线](performance.md)。

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
