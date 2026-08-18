# 硬件与系统兼容性矩阵

这份矩阵只记录真实证据,不把 CI、文档适配或镜像能构建等同于硬件可用。Python 和
Server 镜像的自动化范围仍以[部署支持矩阵](deploy.md#0-支持矩阵)为准;本文聚焦 Agent
主机、USB、Air780E 固件和运营商链路。

## 验证等级

| 等级 | 必须满足 |
| --- | --- |
| 枚举 | 内核加载 `cdc_acm`,每个 `19d1:0001` 完整枚举 `02/04/06`,且设备节点对报告账户可读写;报告 `enumeration_ready=true` |
| AT | 枚举通过,且 `ATI`、`CGMI`、`CGMM`、`CGMR` 返回;报告 `validation_ready=true` |
| 端到端 | AT 通过,再完成一条入站短信、一条出站短信和一次 Server 通知 |
| 恢复 | 端到端通过,再完成冷启动、热插拔/重新编号和 Agent 重连 |

“待验证”表示没有证据,不是已知不兼容。只有达到“端到端”的组合才进入支持范围;
“枚举”或“AT”只用于定位失败层级。

## 已有记录

| ID | 等级 | 日期 | Agent 主机 | Python | 模块 / 固件 | USB / ACM | 数量 | ModemManager | 结果与证据 |
| --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- |
| `arch-x86_64-v1011` | 端到端（历史） | 2026-08-03 至 2026-08-16 | Arch Linux rolling, kernel 7.1.5, x86_64 | 3.14.6 | AirM2M 780EPV / `AirM2M_780EPV_V1011_LTE_AT` | `19d1:0001`,每模块 3 个 ACM（02/04/06）,02 接收 `+CMTI` | 2 | 未安装 | 两张漫游 SIM 同时在线,收发、通知、断线重连通过;存储 `SM`/`ME` 均为 10。记录早于 JSON 报告格式,细节见 [AT 笔记](at-reference.md) |
| `arch-x86_64-v1011-giffgaff-20260817` | AT（出站失败） | 2026-08-17 | 同上 | 同上 | 同上 | 双模块在线 | 2 | 未安装 | giffgaff 卡 EPS/LTE 已注册并附着、CS 与 IMS 未注册（`+CIREG: 2,0`）;按约束只执行一次 `AT+CMGS`,发送失败且网站未出现新出站记录。同卡在手机中可发送,未继续重试 |
| `arch-x86_64-v1011-giffgaff-20260818` | AT（出站失败,已定位到网络侧） | 2026-08-18 | 同上 | 同上 | 同上 | 同上 | 2 | 未安装 | 用第三个 ACM 口旁路复测,不打断 Agent。逐项排除本地成因后判定为网络侧不放通漫游 MO 短信,详见 [AT 笔记 §2.1](at-reference.md#2026-08-18-出站失败的逐项排除)。同批复测还发现两处固件怪癖（`AT+CEREG?` 用 `+CGREG:` 作答、`AT+CMEE=1` 仍回文本），已在 Agent 中处理 |

历史行保留是为了不丢失已经完成的实测,但它没有机器生成的报告附件。下一次维护窗口应
用下述命令补采,在补采前不把它升级为“恢复”等级。

2026-08-17 的失败记录不会抹掉此前成功记录,但说明“同一主机和模块曾成功”不足以证明
当前 SIM、漫游网络和固件组合仍可用。当前 giffgaff 出站链路不列入端到端支持范围。

2026-08-18 的复测把成因定位到网络侧:SMSC、PIN、注册与附着、信号、射频、`AT+CSMS`
能力、存储余量和 PDU 编码逐项确认正常,同一模块入站正常,三次复现出三种不同症状且
对端始终没有收到,说明消息没有进入网络。**因此不再把升级固件当作首要动作** ——
`uart_sms_forwarder` 的 LuatOS 个案只是定位线索,而且切到 LuatOS 后不能继续使用
当前 AT Agent。下一步是在运营商侧确认该资费是否放通漫游 MO 短信;若确认已放通而
仍失败,再回到固件路线,从合宙确认 Air780EPV 的匹配固件和回退包。

2026-08-16 曾在受限开发环境运行 schema v2 的只读枚举采样:sysfs 识别到两个
`19d1:0001`,各有完整的 `02/04/06`,运行时 udev 数据库可读且未应用
`ID_MM_DEVICE_IGNORE` / `ID_MM_PORT_IGNORE`;但该环境隐藏了 `/dev/ttyACM*` 和
systemd 总线,所以报告按设计给出 `device_nodes_ready=false`、
`enumeration_ready=false`。这只能补充内核拓扑证据,不能提升表中验证等级。

## 待扩充组合

| 优先级 | 组合 | 当前状态 | 需要补的证据 |
| ---: | --- | --- | --- |
| 1 | Arch rolling + V1011 + 双模块 | 历史端到端;giffgaff 当前出站失败,已定位到网络侧 | 运营商侧确认漫游 MO 短信是否放通、JSON 报告和一次受控收发;仅在确认已放通而仍失败时才补匹配固件与升级/回退记录 |
| 2 | Debian stable x86_64 + V1011 | 仅文档适配 | `dialout` 权限、报告、收发、systemd 重启 |
| 3 | Ubuntu LTS x86_64 + V1011 + ModemManager | 未实测 | ignore 规则生效前后、报告、20 分钟稳定 AT 与收发 |
| 4 | arm64 Linux + V1011 | 仅 Server 镜像可构建 | Agent 串口、报告和端到端收发 |
| 5 | 其他 Air780E AT 固件 | 未实测 | 固件完整 `CGMR`、报告、收发、存储容量和注册恢复 |

## 生成脱敏报告

schema v2 报告不输出主机名、IMEI、ICCID、SMSC、运营商名称或 USB serial 原值。
它按 USB 物理设备归组 ACM 接口,明确校验每个 Air780E 是否恰好具有
`02/04/06`,并从 `/run/udev/data` 读取已经应用到设备和端口的
`ID_MM_DEVICE_IGNORE` / `ID_MM_PORT_IGNORE`。`modem_manager_ignore_rule_detected`
只说明主机上找到规则文本,`modem_manager_ignore_applied` 才是本次枚举的实际属性。

### 在线枚举与冷启动证据

只看内核、sysfs、udev 和设备节点时使用 `--enumeration-only`;它不会打开任何串口,
可以与生产 Agent 同时运行：

```bash
sudo -u air780e /opt/air780e-agent/.venv/bin/air780e-probe \
  --report /tmp/air780e-enumeration-$(date +%F).json \
  --enumeration-only
```

报告同时记录脱敏的启动会话哈希、系统 uptime 和 `air780e-agent.service` 状态。
冷启动验证时,重启主机后尽早执行上述命令并归档结果;只有 `cdc_acm` 已加载、至少一个
Air780E、每个设备恰好为 `02/04/06` 且所有 `/dev/ttyACM*` 节点对报告账户可读写时,
`enumeration_ready=true` 且退出码为 0。它不读取 AT 身份,因此不能单独证明两个
worker 已按 IMEI/ICCID 认回各自模块。

### 完整 AT 报告

完整报告只执行状态与身份查询,不运行 `CMGL`、`CMGR` 或 `CMGD`,因此不会读取或删除
收件箱。但它会打开每个候选串口,不能和 Agent 并发。应在短维护窗口用子 shell 保证
探测结束后一定重新启动服务：

```bash
(
  set -e
  trap 'sudo systemctl start air780e-agent' EXIT
  sudo systemctl stop air780e-agent
  sudo -u air780e /opt/air780e-agent/.venv/bin/air780e-probe \
    --report /tmp/air780e-compat-$(date +%F).json
)
```

`validation_ready=true` 且退出码为 0 表示枚举完整,并且至少一个 AT 口真实返回了
固件版本。即使失败,JSON 仍会写出:例如只有 sysfs 接口、缺少 `/dev/ttyACM*`、
ACM 接口不是完整的 `02/04/06`,或 `CGMR` 不受支持,都会留下可诊断证据并返回非零。

### 热插拔与重新编号

下面的观察同样不打开串口,可与 Agent 并行。启动命令后在 120 秒内拔下并重新插入
一个模块：

```bash
sudo -u air780e /opt/air780e-agent/.venv/bin/air780e-probe \
  --report /tmp/air780e-hotplug-$(date +%F).json \
  --observe-hotplug 120 --poll-interval 0.5
```

报告只在拓扑变化时追加记录,并分别给出 `disappearance_observed`、
`reappearance_observed`、`tty_renumbering_observed`、`usb_path_change_observed` 和
`topology_restored`。观察到设备消失、重新出现且双模块恢复完整 `02/04/06` 与设备
节点后,`hotplug_cycle_complete=true` 且退出码为 0。`ttyACM` 编号不一定每次都改变,
所以重新编号是独立证据,不是成功的必要条件。

这个报告只证明 USB 拓扑恢复。还需在 Web 设备页或 Agent 日志确认原 worker 按
IMEI/ICCID 恢复、端口路径已更新,再补一条真实收发记录,才能提升到“恢复”等级。

报告默认可分享,提交前仍应人工检查一次。建议文件名使用
`<日期>-<发行版>-<架构>-<固件>.json`,并与以下人工结果一起记录：

1. 入站短信到达 Agent、Server 和通知渠道;
2. 出站短信被目标号码接收;
3. 冷启动枚举报告,以及设备自动上线的 UI/日志证据;
4. 热插拔观察报告,以及同一 IMEI/ICCID 的 worker 自动恢复证据;
5. 若安装 ModemManager,记录 ignore 规则是否生效及持续运行结果。

报告证明查询当时的主机和模块状态,不能替代运营商收发或长稳观察。
