# 推送渠道参考

短信入库后由 **server** 发出推送(不走 SIM 卡流量,决策 D5)。
一条短信要被推出去需要两样东西:一个**渠道**(推到哪)和一条**规则**(什么短信推)。
**没有规则就不会推送任何短信** —— 这是默认安全的一侧。

本文中的 URL、Key、Token、Webhook、号码和消息正文均为占位示例,不得替换为真实凭据后提交。

---

## 1. 规则

| 字段 | 说明 |
|---|---|
| 适用的卡 | 留空 = 全部卡;否则只匹配该 SIM 的短信(挂在 `sim_id` 上,换模块不影响) |
| 匹配方式 | `全部短信` / `包含关键词`(大小写不敏感)/ `正则匹配`(Python 语法,`re.search`) |
| 推送到 | 目标渠道 |
| 消息模板 | 留空用默认;变量见下 |
| 优先级 | 同一渠道被多条规则命中时,数字大的那条决定模板 |

两条注意:

- **同一渠道只推一次。** 「全部短信 → Bark」加「关键词 验证码 → Bark」是常见配法,用户要的是一条推送而不是两条。取优先级最高的那条规则的模板。
- **坏正则只废掉它自己。** 正则写错(比如 `[unclosed`)时,该规则被跳过并记一条 WARNING,其余规则照常推送。
- 关键词规则**留空关键词等于不匹配任何短信**,不是匹配全部 —— 表单填一半不该变成消防水管。

### 模板变量

| 变量 | 内容 |
|---|---|
| `{message}` | 短信正文 |
| `{sender}` | 发件人号码 |
| `{card}` | 卡名。依次取:SIM 备注名 → 手机号 → 模块标签 → ICCID 后四位 |
| `{timestamp}` | 短信时间,按 `HUB_TZ`(默认 `Asia/Shanghai`)渲染 |
| `{device}` | 模块槽位名(`a` / `b`) |
| `{iccid}` | ICCID |

默认模板是 `【{card}】{sender}\n{message}`。

未知的占位符**原样保留**(`{nope}` 就显示 `{nope}`),这样拼错能一眼看见。
短信正文里出现花括号不会影响渲染 —— 模板替换只扫模板本身,不解释正文。

### 规则调试器

通知页可以选择一张卡,填入发件号码和短信正文后预览命中结果。预览**不会访问任何推送服务商**,但与真实发送共用同一套规则去重和载荷渲染逻辑:

- 同一渠道只显示优先级最高的命中规则;
- 标题使用渠道配置的 `title`,正文使用规则模板;
- `{card}` 使用所选 SIM 的备注名 / 手机号 / ICCID;
- `{timestamp}` 按 `HUB_TZ` 渲染。

因此调试器展示的标题和正文就是该消息实际推送时会生成的内容。

---

## 2. 渠道配置

界面上按类型显示对应字段,下面是它们落到 `config` JSON 里的键名。

### bark(iOS)

| 键 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | `https://api.day.app/你的key`,自建服务器同理 |

发 `POST {url}`,JSON `{"title": …, "body": …}`。

### telegram

| 键 | 必填 | 说明 |
|---|---|---|
| `token` | 是 | @BotFather 给的 bot token |
| `chat_id` | 是 | 个人 chat id 或群 id(群 id 带负号) |

### feishu(飞书自定义机器人)

| 键 | 必填 | 说明 |
|---|---|---|
| `webhook` | 是 | `https://open.feishu.cn/open-apis/bot/v2/hook/...` |
| `secret` | 否 | 机器人安全设置勾了「签名校验」才填 |

加签算法:以 `"{timestamp}\n{secret}"` 为 key、对**空字符串**做 HMAC-SHA256,base64 后连同
`timestamp` 一起放进请求体。填了 `secret` 才带,不填就是纯 webhook。

### wecom(企业微信群机器人)

| 键 | 必填 | 说明 |
|---|---|---|
| `webhook` | 是 | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` |

### dingtalk(钉钉自定义机器人)

| 键 | 必填 | 说明 |
|---|---|---|
| `webhook` | 是 | `https://oapi.dingtalk.com/robot/send?access_token=...` |
| `secret` | 否 | 安全设置选「加签」时填 |

加签算法与飞书不同:以 **secret 为 key**、对 `"{毫秒时间戳}\n{secret}"` 做 HMAC-SHA256,
base64 后作为 query 参数 `sign`(连同 `timestamp`)拼到地址上,地址原有的 `access_token` 保留。

> 钉钉机器人的安全设置三选一。若选的是「自定义关键词」,把关键词写进规则模板里,
> 否则会收到 `errcode 310000: keywords not in content`。

### post(自定义 POST)

| 键 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | 接收 JSON 的地址 |
| `headers` | 否 | 附加请求头对象,例如 `{"X-Token": "abc"}`(目前只能通过 API 直接写入) |

请求体:

```json
{
  "sender": "10086", "message": "验证码 123456", "card": "移动保号卡",
  "timestamp": "2026-08-02 18:00:00", "device": "a", "iccid": "8986...",
  "title": "移动保号卡 · 10086", "text": "渲染后的模板文本"
}
```

### get(自定义 GET)

| 键 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | 参数会**并入**地址已有的 query(所以 URL 里可以先带上 API key) |

### smtp(邮件)

| 键 | 必填 | 说明 |
|---|---|---|
| `host` | 是 | 如 `smtp.qq.com` |
| `to` | 是 | 收件人,多个用逗号分隔 |
| `security` | 否 | `ssl`(默认)/ `starttls` / `none` |
| `port` | 否 | 留空时 ssl 用 465、其余用 587 |
| `username` / `password` | 否 | 多数服务商这里要填「授权码」而不是登录密码 |
| `from` | 否 | 留空用 `username` |

邮件走 `smtplib`,**在线程池里发**:SMTP 是阻塞的,而 WebSocket 网关和它共用一个事件循环。

### 通用可选项

| 键 | 说明 |
|---|---|
| `title` | 标题模板,用于 Bark 标题与邮件主题。默认 `{card} · {sender}` |

---

## 3. 失败与重试

- 每个渠道**独立重试**,默认总共 3 次(1 次 + `HUB_NOTIFY_RETRIES=2`),退避 1s / 3s
- 每次投递(成功或最终失败)都在 **日志 → 推送日志** 留一行,含尝试次数与失败原因
- 界面上的「测试」按钮**只发一次不重试** —— 人在等结果,要的是服务商此刻的原话

**HTTP 200 不等于成功。** 这几家都会用 200 回错误码,引擎逐个判:

| 渠道 | 成功条件 |
|---|---|
| bark | `code == 200` |
| telegram | `ok == true` |
| wecom / dingtalk | `errcode == 0` |
| feishu | `code == 0` 或 `StatusCode == 0` |
| post / get | HTTP 2xx |

判失败时把服务商自己的 `msg` / `errmsg` / `description` 抄进推送日志,直接告诉你钉钉说的是
"keywords not in content" 还是飞书说的 "sign match fail"。

---

## 4. 日志里没有的东西

这套系统经手全部验证码,所以(PLAN.md §10):

- 推送日志**只记状态码和服务商错误文本,不记短信正文**
- 错误信息里的 URL 会被替换成 `<url>` —— Telegram 的 bot token 就在 URL 路径里,
  而推送日志是要在浏览器里显示的

---

## 5. 相关环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HUB_NOTIFY_RETRIES` | `2` | 首次失败后的重试次数 |
| `HUB_NOTIFY_TIMEOUT` | `10` | 单次 HTTP 请求超时(秒) |
| `HUB_TZ` | `Asia/Shanghai` | `{timestamp}` 用的时区 |
