# 部署指南

air780e-hub 分为两个独立进程：

- **Server**：部署在可被 Agent 和浏览器访问的主机上，推荐使用 Docker Compose。
- **Agent**：部署在连接 Air780E USB 模块的 Linux 主机上，推荐使用 systemd。

本文所有域名、Token、IMEI 和 ICCID 都是占位符。不要把真实配置、数据库、日志或凭据保存到仓库中。

## 0. 支持矩阵

分两级陈述：**CI 覆盖**由每个 Pull Request 自动验证；**实机验证**只能靠真实硬件
记录，范围小得多。矩阵之外的组合不承诺可用。

| 维度 | 范围 | 依据 |
| --- | --- | --- |
| Python | 3.11 – 3.14 | CI 对 Agent 与 Server 在四个版本上各跑全量测试 |
| Server 运行时 | 镜像内固定 `python:3.12-slim` | 源码部署可用 3.11 – 3.14 任一 |
| Agent 运行时 | 发行版自带 `python3` | 按 §2.1 用系统解释器建 venv，故上限跟随最新稳定版 |
| Node | 24（active LTS，仅构建期） | 前端运行时是静态文件，部署主机不需要 Node |
| 架构 | amd64 / arm64 | GHCR 多架构镜像 |

**实机验证状态（0.1.0）**：仅一套环境跑通端到端 —— Arch Linux、Python 3.14、
两个 Air780E（固件 `AirM2M_780EPV_V1011_LTE_AT`）、两张漫游 SIM。

未实测但已按文档适配的部分：

- **Debian / Ubuntu**：串口属组是 `dialout` 而非 Arch 的 `uucp`，systemd 单元里
  需要相应调整（见 §2.5），但没有实机记录；
- **arm64 镜像**：能构建并推送，没有在真机上运行过；
- **其他 Air780E 固件版本**：只验证过 `V1011`。

### 版本与兼容性

遵循 SemVer。`0.x` 阶段**不承诺向后兼容**：minor 升级（`0.1` → `0.2`）可能改动
数据库 schema 与协议帧，每次都会在 [CHANGELOG](../CHANGELOG.md) 显式标注并给出
升级步骤；patch 升级（`0.1.0` → `0.1.1`）只含修复。

**Agent 与 Server 必须同版本部署。** 跨版本混用不受支持 —— 两端版本不一致时，
运维中心会直接显示「版本不同」或「协议不兼容」并创建事件。协议不兼容表示两端对
帧字段或语义的理解可能不同，应按故障处理。升级步骤见 §1.5（Server）和 §2.6（Agent）。

## 1. 部署 Server

### 1.1 前置条件

- Docker Engine 24+
- Docker Compose v2
- 支持 HTTPS 和 WebSocket 的反向代理
- 可备份的持久化存储

### 1.2 构建并启动

两条路径都受支持。**源码构建是参考路径** —— 不需要访问任何 registry：

```bash
git clone https://github.com/wildalley/air780e-hub.git /opt/air780e-hub
cd /opt/air780e-hub
docker compose -f deploy/docker-compose.yml up -d --build
```

也可以用已发布的镜像（amd64 / arm64）。**按摘要固定，不要用 tag**：摘要唯一对应一份字节，而 `X.Y` 会随补丁版本移动，回滚时无法指明恢复到哪一版。摘要在对应 [Release](https://github.com/wildalley/air780e-hub/releases) 正文里：

```bash
export HUB_IMAGE=ghcr.io/wildalley/air780e-hub@sha256:<摘要>
docker compose -f deploy/docker-compose.yml up -d
```

每个 Release 附带 SPDX SBOM 和构建来源证明，可校验镜像确由本仓库的 tag 构建：

```bash
gh attestation verify oci://ghcr.io/wildalley/air780e-hub@sha256:<摘要> \
  --repo wildalley/air780e-hub
```

默认监听：

```text
127.0.0.1:8090 -> container:8080
```

可通过环境变量调整宿主机监听地址和端口：

```bash
HUB_BIND_ADDRESS=127.0.0.1 HUB_HOST_PORT=8090 \
  docker compose -f deploy/docker-compose.yml up -d --build
```

只有当反向代理运行在无法访问宿主机回环地址的独立容器中，才应设置 `HUB_BIND_ADDRESS=0.0.0.0`。这种情况下必须使用防火墙或私有容器网络阻止公网直接访问明文 HTTP 端口。

### 1.3 数据与凭据

Server 把 SQLite 数据库、Agent Token 和运行时设置保存在 `hub-data` volume 的 `/data` 中。

读取自动生成的 Agent Token：

```bash
docker exec air780e-hub hub-server token
```

该 Token 等同于 Agent 接入凭据：

- 不要写入 README、Issue、截图或命令历史共享记录；
- 不要放进 Git 版本控制；
- 泄露后应立即轮换并更新所有 Agent；
- 备份 `hub-data` 时按敏感数据处理。

首次访问 Web 管理端时，系统会要求设置管理员密码。系统不提供免密模式。

#### 轮换 Agent Token

在 Web 管理端的“设置”页可以轮换 Agent Token，并为旧 Token 设置有限宽限期。
推荐顺序如下：

1. 选择足以更新全部 Agent 的最短宽限期并执行轮换；
2. 将新 Token 写入每台 Agent 的仓库外配置，保持文件权限为 `0600`；
3. 逐台重启 Agent，并在“运维中心”确认重新连接；
4. 使用新 Token 运行部署自检；
5. 宽限期结束后确认没有 Agent 离线，再安全销毁旧 Token 副本。

系统只保留紧邻的上一个 Token；再次轮换会立即废止更早的 Token。若通过
`HUB_AGENT_TOKEN` 环境变量提供 Token，Web 管理端不能在线轮换，应修改部署环境并
重启 Server，同时协调更新所有 Agent。

### 1.4 反向代理

生产环境必须使用 HTTPS。以下 Nginx 示例同时代理普通 HTTP 和 WebSocket：

```nginx
server {
    listen 443 ssl http2;
    server_name sms.example.com;

    # ssl_certificate /path/to/fullchain.pem;
    # ssl_certificate_key /path/to/private-key.pem;

    location /ws {
        proxy_pass http://127.0.0.1:8090/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`X-Forwarded-Proto` 用于判断是否设置 Secure Cookie；遗漏后可能出现登录后立即退出。Agent 使用 `wss://sms.example.com/ws`，因此 `/ws` 必须正确传递升级头。

健康检查：

```bash
curl --fail https://sms.example.com/healthz
```

使用仓库自带的自检器验证完整入口。它读取 Token 后只在内存中使用，不会把
Token 打印到输出，也不会创建临时 Agent 记录：

```bash
token_file=$(mktemp)
trap 'rm -f "$token_file"' EXIT
chmod 600 "$token_file"
docker exec air780e-hub hub-server token >"$token_file"
python3 deploy/self_check.py \
  --url https://sms.example.com \
  --token-file "$token_file"
```

检查器失败时会分别报告 `/healthz`、TLS/升级头和 Token 校验。局域网直连的
明文临时验收可以使用 `--allow-http`，不应将该选项用于公网部署。

WebSocket 握手检查（把占位符替换为本机安全保存的 Token，不要把真实值提交到文件）：

```bash
curl -i -N \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  -H "Authorization: Bearer <AGENT_TOKEN>" \
  https://sms.example.com/ws
```

期望状态为 `101 Switching Protocols`。

### 1.5 升级与备份

升级：

```bash
cd /opt/air780e-hub
git pull --ff-only
docker compose -f deploy/docker-compose.yml up -d --build
```

Server 启动时会按 `PRAGMA user_version` 顺序迁移数据库；升级前自动在同一数据目录写入
`hub.db.v<旧版本>.bak`。确认历史短信、任务和规则正常后再清理旧快照，不要在迁移期间
同时运行旧 Server 实例。

升级不会删除 `hub-data`。仍应定期执行以下任一备份：

- 在 Web 管理端下载 SQLite 快照；
- 对 `hub-data` volume 做一致性备份；
- 停止容器后复制 volume 内容。

备份包含短信正文、号码、会话和推送凭据，必须加密并限制访问。

### 1.6 运维中心

Web 管理端的“运维中心”每 15 秒刷新一次，集中展示 Server 运行时间、数据库与
磁盘占用、Agent 连接状态、待处理命令、发送中的通知和离线观察计时器。

系统会为以下九种情况创建事件：模块离线、网络未注册、短信发送失败、保号任务
失败、通知投递失败、SIM 余额不足、SIM 套餐到期、SIM 保号期限和 Agent / Server 版本不一致。事件按模块、SIM、
任务、渠道或 Agent 聚合，重复发生只累加次数，不会产生新行。管理员可以确认或解决
事件；对应状态恢复时（模块重新在线、注册成功、发送成功、任务成功、通知成功、SIM
余额恢复、续期或更新保号截止日、两端版本恢复一致），系统会自动解决仍未关闭的事件。模块离线时，它的
未注册和发送失败事件会一并关闭 —— 这两种状态在模块离线期间无法恢复，且已由
掉线事件表达。

SIM 页面的计费方式、套餐、余额、币种、低余额阈值和两个期限均由管理员维护。不同国家和运营商
没有统一的账户查询接口，USSD 与账务短信格式也不稳定，因此系统不会把它们自动解析
为余额；每次保存新余额时，Server 会记录余额更新时间。当前余额不高于阈值时建立
低余额事件，余额为 0 或负数时升级为 critical。余额、套餐到期日与保号截止日使用
独立事件，修改其中一个不会关闭另外两个。

设备页可以按模块切换移动网络射频。关闭后模块保持在线和 AT 串口可控，但不再注册
运营商网络；系统把它视为管理员主动进入飞行模式，不创建未注册事件。该模块的保号
任务会跳过且不消耗重试，重新开启后由后续状态采样跟踪注册恢复。

保号任务页每行的播放按钮可以手动触发一次任务，停用任务也适用。请求只确认 Agent
已开始异步执行，实际重试、任务日志和结果通知会在执行完成后按常规 `task_result`
链路更新；同一任务已经运行时不会重复启动。

“管理审计”记录 API 变更操作、结果、耗时和来源地址，但不会保存请求正文、短信
内容或凭据。审计中间件运行在认证之前，因此未命中真实路由的请求不会写入审计，
以免任何能访问端口的人凭空造出审计行；失败的登录尝试命中真实路由，会被记录。
审计记录不能替代反向代理访问日志和主机级安全日志，生产环境仍应按既定保留策略
集中保存。

### 1.7 保留策略

短信、状态采样、日志、审计和已解决事件都是只增表，各自有独立的保留期，通过
容器环境变量设置（设为 `0` 表示不删除）：

| 变量 | 默认 | 作用范围 |
| --- | --- | --- |
| `HUB_MESSAGE_RETENTION_DAYS` | 90 | 短信正文与会话 |
| `HUB_STATUS_RETENTION_DAYS` | 30 | 设备状态采样 |
| `HUB_LOG_RETENTION_DAYS` | 30 | Agent 日志、任务日志、通知日志 |
| `HUB_AUDIT_RETENTION_DAYS` | 180 | 管理审计 |
| `HUB_INCIDENT_RETENTION_DAYS` | 90 | 已解决的事件 |
| `HUB_AUDIT_MAX_ROWS` | 200000 | 审计表行数上限，在按期清理之后执行 |

未解决的事件不受保留期影响，无论多旧都会保留 —— 它仍然是系统当前状态的一部分。
删除短信时，与之关联的通知日志会随之级联删除；任务结果和渠道测试产生的通知日志
没有关联短信，由 `HUB_LOG_RETENTION_DAYS` 负责。

清理每 6 小时自动执行一次，也可以在“设置 → 数据清理”里手动触发。

## 2. 部署 Agent

### 2.1 安装

以下示例把 Agent 安装到 `/opt/air780e-agent`。运行时依赖只有 pyserial 和
websockets，都是纯 Python 包，系统自带的 `python3`（`>=3.11`）足够，无需额外
工具链：

```bash
sudo mkdir -p /opt/air780e-agent
sudo rsync -a --delete \
  --exclude '.venv' --exclude 'tests' --exclude '.tmp' \
  --exclude '__pycache__' --exclude '.*_cache' \
  agent/ /opt/air780e-agent/
sudo python3 -m venv /opt/air780e-agent/.venv
sudo /opt/air780e-agent/.venv/bin/pip install /opt/air780e-agent
```

必须排除 `.venv`：仓库 `agent/` 目录里可能有一个开发用虚拟环境，它的
`bin/python3` 指向 `$HOME` 下的解释器。拷进部署目录会覆盖掉部署环境，而
systemd 单元设了 `ProtectHome=true`，服务随后只会以 `203/EXEC`
（Permission denied）反复重启。同理不要用 `uv venv` 创建这个环境 —— uv 自带的
解释器也装在 `$HOME` 下，会踩到同一个坑。

也可以在仓库的 `agent/` 目录中直接使用 `uv run` 运行，这条路径不经过 systemd，
不受上述限制。

### 2.2 串口权限

Air780E 通常枚举为多个 `/dev/ttyACM*`。常见串口组：

- Arch Linux：`uucp`
- Debian / Ubuntu：`dialout`

确认实际属组：

```bash
ls -l /dev/ttyACM*
```

将运行 Agent 的服务账户加入对应组。若安装了 ModemManager，应停止其占用这些串口，或应用 [`deploy/udev/99-air780e.rules`](../deploy/udev/99-air780e.rules) 中的忽略规则。

### 2.3 识别模块

Agent 可以按 IMEI 或 ICCID 自动发现模块，无需依赖不稳定的 `ttyACM` 编号：

```bash
/opt/air780e-agent/.venv/bin/python -m air780e_agent.probe --scan
/opt/air780e-agent/.venv/bin/python -m air780e_agent.probe /dev/ttyACM0
```

如果需要固定符号链接，可安装 udev 示例并在配置中显式设置 `port`。

### 2.4 配置

生成示例：

```bash
sudo mkdir -p /etc/air780e-agent
/opt/air780e-agent/.venv/bin/air780e-agent --print-example-config \
  | sudo tee /etc/air780e-agent/config.toml >/dev/null
sudo chmod 600 /etc/air780e-agent/config.toml
sudoedit /etc/air780e-agent/config.toml
```

至少需要修改：

```toml
[agent]
id = "site-a"

[server]
url = "wss://sms.example.com/ws"
token = "<AGENT_TOKEN>"

[[devices]]
name = "modem-a"
label = "SIM A"
imei = "<MODEM_IMEI>"
```

也可以用 `iccid = "<SIM_ICCID>"` 按卡认领。真实配置只能保存在版本库外，并保持 `0600` 权限。

Agent 默认开启设备自愈，通常无需调整。可选参数均位于 `[agent]`：

| 参数 | 默认值 | 作用 |
| --- | ---: | --- |
| `health_check_timeout` | `5.0` | 基础 `AT` 健康探测的单次超时（秒） |
| `health_failure_threshold` | `3` | 连续失败多少次后关闭并重开串口 |
| `registration_recovery_delay` | `300.0` | 持续未注册多久后开始自动恢复（秒） |
| `recovery_cooldown` | `300.0` | 两次网络恢复动作的最短间隔（秒） |
| `recovery_max_attempts_24h` | `6` | 滚动 24 小时内最多执行多少次网络恢复动作；`0` 表示禁用 |

网络恢复按“自动选择运营商 → 射频重启 → 模块重启”逐级执行。次数和当前阶段保存在
Agent 本地 SQLite 中，进程重启不会绕过限频；管理员主动关闭射频时不会触发恢复。

校验配置但不访问硬件：

```bash
/opt/air780e-agent/.venv/bin/air780e-agent \
  --check --config /etc/air780e-agent/config.toml
```

### 2.5 systemd

创建服务账户、复制示例服务并按发行版检查串口组。下面使用 Arch Linux 的 `uucp`；Debian / Ubuntu 应改为 `dialout`：

```bash
sudo useradd --system --no-create-home --groups uucp air780e
sudo cp deploy/systemd/air780e-agent.service /etc/systemd/system/
sudo systemctl edit air780e-agent
```

确认 unit 中的 `Group`、`SupplementaryGroups` 和安装路径后启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now air780e-agent
journalctl -u air780e-agent -f
```

### 2.6 升级

Agent 和 Server 各自独立升级，跨版本的新字段通常需要两端都更新。升级 Agent：

```bash
cd /path/to/air780e-hub
git pull --ff-only
sudo rsync -a --delete \
  --exclude '.venv' --exclude 'tests' --exclude '.tmp' \
  --exclude '__pycache__' --exclude '.*_cache' \
  agent/ /opt/air780e-agent/
sudo /opt/air780e-agent/.venv/bin/pip install /opt/air780e-agent
sudo systemctl restart air780e-agent
```

`pip install` 这一步不能省。服务运行的是 `.venv/.../site-packages/` 里的副本，
不是 `/opt/air780e-agent/src/`；只同步源码不重装，跑的仍是旧代码，而且两者的
文件时间戳会让人误以为已经升级过了。确认实际生效的版本：

```bash
sudo /opt/air780e-agent/.venv/bin/python3 -c \
  'import air780e_agent, os; print(os.path.dirname(air780e_agent.__file__))'
```

重启会断开串口和 WebSocket 数秒。已入队但未上报的事件保存在 Agent 本地
SQLite（`[agent].db`）中，重连后补传，不会因重启丢失。

升级后打开运维中心检查 Agent 行：应显示与 Server 相同的软件版本和绿色的协议版本。
「版本不同」说明只更新了一端；「协议不兼容」还表示线协议不同，必须先完成两端升级
再继续使用短信与任务功能。

## 3. 验收

按以下顺序验证：

1. `https://sms.example.com/healthz` 返回成功；
2. Agent 日志出现 `link established`；
3. Web 仪表盘显示模块在线，IMEI / ICCID 与本机设备一致；
4. 向 SIM 发送测试短信，Web 会话中出现该消息；
5. 从 Web 回复，目标手机收到短信；
6. 配置测试通知渠道和规则，确认脱敏日志记录成功状态；
7. 暂时断开 Agent 网络，注入消息后恢复，确认事件完整补传。
8. 在测试模块上模拟未注册或 AT 无响应，确认 Agent 自动恢复，且运维中心事件随后自动关闭。
9. 为测试 SIM 分别设置 30 天内的套餐到期日和保号截止日，确认运维中心产生两个事件；逐一延长到 30 天外，确认对应事件独立关闭。
10. 为测试 SIM 设置低余额阈值，将余额依次改为阈值以上、阈值以内和 0，确认事件依次为关闭、warning 和 critical；充值到阈值以上后确认自动关闭。

测试短信应使用专用号码和无敏感内容，不要把真实验证码用于截图、Issue 或公开日志。

## 4. 常见问题

### Agent 无法连接 Server

- 检查 `server.url` 是否使用 `wss://`；
- 检查证书链和 DNS；
- 检查反向代理是否传递 WebSocket 升级头；
- 检查 Token 是否一致；
- 使用 `curl https://sms.example.com/healthz` 验证普通 HTTP 链路。

### WebSocket 返回 HTTP 200 / 404 / 502

反向代理没有把 `/ws` 当作 WebSocket，或上游地址错误。检查 `Upgrade`、`Connection` 和 `proxy_http_version 1.1`。

### 模块一直离线

- 检查 Agent 日志；
- 检查串口属组和服务账户权限；
- 确认 ModemManager 未占用端口；
- 使用 `probe --scan` 验证 AT 口；
- 检查配置中的 IMEI / ICCID 是否与模块一致。

### Agent 反复以 203/EXEC 重启

```
Failed to execute /opt/air780e-agent/.venv/bin/air780e-agent: Permission denied
air780e-agent.service: Failed at step EXEC ... status=203/EXEC
```

虚拟环境的解释器指向了 `$HOME` 之下，而单元设有 `ProtectHome=true`，服务读不到
它。几乎总是因为把仓库 `agent/.venv` 拷进了部署目录，或用 `uv venv` 创建了环境。
检查符号链接指向哪里：

```bash
ls -l /opt/air780e-agent/.venv/bin/python3
```

若指向 `/home/...`，用系统解释器重建：

```bash
sudo rm -rf /opt/air780e-agent/.venv
sudo python3 -m venv /opt/air780e-agent/.venv
sudo /opt/air780e-agent/.venv/bin/pip install /opt/air780e-agent
sudo systemctl restart air780e-agent
```

### 运维中心显示版本不同或协议不兼容

按 2.6 升级并重新安装 Agent，按 1.5 升级 Server，确保两端来自同一版本。若只同步
了 `agent/src/` 而没有在服务使用的虚拟环境中重新执行 `pip install`，进程仍会报告
旧版本；检查包的实际导入路径后重启服务。

### 登录后立即退出

确认反向代理传递 `X-Forwarded-Proto: https`，并且浏览器只通过 HTTPS 访问。

### 忘记管理员密码

```bash
docker exec -it air780e-hub hub-server auth reset-password
```

如需清空认证状态并在下次访问重新初始化：

```bash
docker exec -it air780e-hub hub-server auth clear
```
