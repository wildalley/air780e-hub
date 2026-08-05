# 发布清单

这份清单把可自动化的发布步骤固定下来，但不替仓库所有者决定许可证、发行方式或支持承诺。当前包和应用版本均为 `0.1.0`，尚未创建正式 Git tag。

## 发布前决策门槛

首个 Release 前必须明确：

1. 许可证，或明确保持专有授权；
2. 发行方式：源码构建、镜像，或两者；
3. 支持矩阵：Python、Node、Linux 发行版、Air780E 型号和 AT 固件；
4. 版本策略和兼容性承诺。

这些决定完成后，把结论同步到 `README.md`、`SECURITY.md` 和本文件，再创建版本号对应的 tag。

## 自动检查

在目标提交上确认 GitHub Actions 的 `CI` 和 `Security` 全部通过：

- Agent / Server 在 Python 3.11、3.12、3.13 上的测试；
- 前端 `npm ci` 与生产构建；
- `docker compose -f deploy/docker-compose.yml config --quiet`；
- 本地 Markdown 链接检查；
- 全 Git 历史 Secret scan；
- Python 与 npm 锁定依赖审计；
- 构建镜像的 HIGH / CRITICAL 漏洞扫描。

再在一台干净的部署主机执行：

```bash
python3 deploy/self_check.py --url https://sms.example.com --token-file /run/secrets/air780e-agent-token
```

输出必须同时包含 health endpoint 和 WebSocket 两项 `PASS`。自检 Token 文件应为
`0600`，检查结束后删除临时副本。

## 版本与制品

1. 更新 `CHANGELOG.md`，将本次变更从 `Unreleased` 移到选定版本，并写明日期。
2. 同步 `agent/pyproject.toml`、`server/pyproject.toml`、`frontend/package.json`、`frontend/package-lock.json` 以及两个 Python 包的 `__version__`。
3. 在干净工作树创建带版本号的提交和 tag；不要把数据库、配置、日志、Token 或前端构建产物加入提交。
4. 按已选发行方式生成制品。若发布容器，生成 amd64 / arm64 镜像并附带 SBOM 和摘要；不要只发布可变的 `latest`。
5. 在全新目录按 [部署指南](deploy.md) 安装制品，完成一次健康检查、WebSocket 自检、登录和短信收发验收。
6. 打开运维中心确认磁盘、Agent 和运行时指标正常，制造并恢复一个可控测试事件，确认事件状态与管理审计均按预期更新。

## 回滚

保留上一版本镜像摘要和数据库备份。升级失败时先停止流量、保留失败日志，再恢复上一个摘要；不要删除 `hub-data` volume。恢复后重新运行部署自检，并确认历史短信和任务数量未变化。
