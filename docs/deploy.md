# 部署

两侧分开部署:**服务器**跑 Docker(1Panel),**本地 Arch** 跑 agent(systemd)。

---

## 一、服务器端(1Panel + Docker)

### 1. 起容器

compose 里用的是 `build:` 而不是现成镜像 —— 前端要在镜像里编译,所以**服务器上必须有整个仓库**,只把 `docker-compose.yml` 贴进 1Panel 的「创建编排」会因为找不到构建上下文而失败。

先把仓库弄到服务器上。仓库是私有的,克隆需要凭证,三选一:

```bash
# a) gh(最省事,交互式登录一次)
gh auth login && gh repo clone wildalley/air780e-hub /opt/air780e-hub

# b) 细粒度 PAT(只给这个仓库的 Contents: Read 权限)
git clone https://<PAT>@github.com/wildalley/air780e-hub.git /opt/air780e-hub

# c) 部署密钥(服务器上生成,公钥贴到仓库 Settings → Deploy keys,只读)
ssh-keygen -t ed25519 -f ~/.ssh/air780e -N ""
git clone git@github.com:wildalley/air780e-hub.git /opt/air780e-hub
```

然后构建启动:

```bash
cd /opt/air780e-hub
docker compose -f deploy/docker-compose.yml up -d --build
```

`build.context` 是相对 compose 文件所在目录的 `..`,也就是仓库根 —— 这样 `server/` 和 `frontend/` 都在上下文里。

想用 1Panel 的编排界面管理,就把 `deploy/docker-compose.yml` 复制成编排目录下的 `docker-compose.yml`,并把 `context` 改成仓库的绝对路径。

**升级**就是 `git pull` 再重跑上面那条 `up -d --build`;数据在具名 volume 里,不受影响。

关于端口绑定有个坑:1Panel 的 OpenResty 一般跑在自己的容器里,所以**不能**把端口绑成 `127.0.0.1:8080` —— 那样 OpenResty 容器访问不到。compose 里用的是 `8080:8080`(发布到宿主机),**同时务必在防火墙上只放行 443,不要放行 8080**。

### 2. 拿 agent token

首次启动会自动生成一个,读出来:

```bash
docker exec air780e-hub hub-server token
```

也可以在 compose 里用 `HUB_AGENT_TOKEN` 自己指定。登录后台后在「系统 → agent token」也能看到。

### 3. 配反向代理

1Panel → **网站 → 创建网站 → 反向代理**:

- 主域名:`sms.example.com`
- 代理地址:`http://<宿主机IP>:8080`

然后申请证书、开启 HTTPS、开启强制跳转。

### ⚠️ 4. 必须打开 WebSocket 支持

**这是唯一一个不配就一定连不上的地方。** agent 走 `wss://<域名>/ws`,如果反代不透传升级头,握手会被降级成普通 HTTP,agent 一直重连失败。

1Panel 较新版本在反向代理配置里有 **WebSocket 支持** 开关,打开即可。

如果没有那个开关,就在网站的 **配置文件** 里手工加(1Panel → 网站 → 你的站点 → 配置文件):

```nginx
location /ws {
    proxy_pass http://<宿主机IP>:8080/ws;

    # 这三行是关键
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # 长连接不能被闲置超时掐断。心跳是 30 秒一次,这里留足余量。
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

`X-Forwarded-Proto` 也别漏 —— 服务器靠它判断请求是不是 HTTPS,进而决定会话 Cookie 要不要带 `Secure` 标记。

**自检:**

```bash
# 期望返回 101 Switching Protocols
curl -i -N \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  -H "Authorization: Bearer <你的token>" \
  https://sms.example.com/ws
```

返回 `101` 就对了。返回 `200`/`404`/`502` 说明升级头没透传。

### 5. 设置管理员密码

浏览器打开 `https://sms.example.com`,首次访问会要求设置管理员密码。

**本项目不提供免密模式** —— 这套系统经手你全部的短信验证码(PLAN.md §10)。

忘记密码时,SSH 到服务器:

```bash
docker exec -it air780e-hub hub-server auth reset-password
docker exec -it air780e-hub hub-server auth clear   # 清空,下次访问重新设置
```

### 6. 备份

数据全在 `hub-data` 这个 volume 的 `/data` 里(SQLite + agent token + 设置)。1Panel 的备份功能直接勾这个 volume 即可。

---

## 二、本地端(Arch + systemd)

### 1. 装 agent

```bash
sudo mkdir -p /opt/air780e-agent
sudo cp -r agent /opt/air780e-agent/
cd /opt/air780e-agent/agent
sudo uv venv .venv && sudo uv pip install --python .venv .
```

### 2. 串口权限与 udev

Arch 的 tty 属 `uucp` 组(不是 Debian 的 `dialout`):

```bash
sudo usermod -aG uucp $USER    # 之后注销重登
ls -l /dev/ttyACM*             # 确认实际属组
```

把 [`deploy/udev/99-air780e.rules`](../deploy/udev/99-air780e.rules) 复制到 `/etc/udev/rules.d/`,**按里面的注释填好实际的 USB 端口路径**,然后:

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
ls -l /dev/air780e-*
```

这一步为什么必须按端口路径而不是序列号绑:见 [`at-reference.md` §3.3](at-reference.md)。

### 3. ModemManager

如果系统装了它,会抢串口:

```bash
sudo systemctl mask ModemManager
```

或者靠上面那份 udev 规则里的 `ID_MM_DEVICE_IGNORE`。

### 4. 配置

```bash
sudo mkdir -p /etc/air780e-agent
air780e-agent --print-example-config | sudo tee /etc/air780e-agent/config.toml
sudo chmod 600 /etc/air780e-agent/config.toml   # 里面有 token
sudo nano /etc/air780e-agent/config.toml
```

填 `server.url = "wss://sms.example.com/ws"` 和第 2 步拿到的 `server.token`。

校验(不碰硬件):

```bash
air780e-agent --check --config /etc/air780e-agent/config.toml
```

### 5. 起服务

```bash
sudo useradd --system --groups uucp --no-create-home air780e
sudo cp deploy/systemd/air780e-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now air780e-agent
journalctl -u air780e-agent -f
```

---

## 三、验证整条链路

1. `journalctl -u air780e-agent -f` 应该看到 `link established`
2. 后台仪表盘上两个模块显示在线
3. 给其中一张卡发条短信,后台短信列表应该秒出
4. 后台点发送,手机应该收到

断网测试(证明 D4 的本地缓冲有效):

```bash
# 拔掉服务器的网 / 停掉容器
docker stop air780e-hub
# 期间给卡发几条短信
docker start air780e-hub
# 后台应该一条不少地补上
```

---

## 常见问题

**agent 日志刷 `cannot reach server`**
反代或域名不通。先用 `curl https://sms.example.com/healthz` 确认。

**agent 日志刷 `server rejected the connection (HTTP 200)`**
WebSocket 升级头没透传 —— 回到上面第 4 步。HTTP 200 是典型症状:反代把它当普通请求处理了。

**agent 日志刷 `authentication failed`**
token 不对。`docker exec air780e-hub hub-server token` 重新取。注意 agent 会退避到 60 秒重试,不会疯狂打服务器。

**后台能登录但设备一直离线**
agent 没连上,或者串口没打开。看 `journalctl -u air780e-agent`。

**登录后马上被登出**
反代没传 `X-Forwarded-Proto`,服务器以为是 HTTP,Cookie 没带 `Secure`,而浏览器在 HTTPS 页面下的处理不一致。补上那行。

**短信收不到但模块在线**
先看后台日志页有没有 `storage x/50` 的告警。再用 `probe` 直连看模块存储:

```bash
python -m air780e_agent.probe /dev/air780e-a
```
