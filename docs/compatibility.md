# 硬件与系统兼容性矩阵

这份矩阵只记录真实证据,不把 CI、文档适配或镜像能构建等同于硬件可用。Python 和
Server 镜像的自动化范围仍以[部署支持矩阵](deploy.md#0-支持矩阵)为准;本文聚焦 Agent
主机、USB、Air780E 固件和运营商链路。

## 验证等级

| 等级 | 必须满足 |
| --- | --- |
| 枚举 | 内核加载 `cdc_acm`,报告记录 USB VID/PID、ACM 接口和设备节点权限 |
| AT | 枚举通过,且 `ATI`、`CGMI`、`CGMM`、`CGMR` 返回;报告 `validation_ready=true` |
| 端到端 | AT 通过,再完成一条入站短信、一条出站短信和一次 Server 通知 |
| 恢复 | 端到端通过,再完成冷启动、热插拔/重新编号和 Agent 重连 |

“待验证”表示没有证据,不是已知不兼容。只有达到“端到端”的组合才进入支持范围;
“枚举”或“AT”只用于定位失败层级。

## 已有记录

| ID | 等级 | 日期 | Agent 主机 | Python | 模块 / 固件 | USB / ACM | 数量 | ModemManager | 结果与证据 |
| --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- |
| `arch-x86_64-v1011` | 端到端（历史） | 2026-08-03 至 2026-08-16 | Arch Linux rolling, kernel 7.1.5, x86_64 | 3.14.6 | AirM2M 780EPV / `AirM2M_780EPV_V1011_LTE_AT` | `19d1:0001`,每模块 3 个 ACM（02/04/06）,02 接收 `+CMTI` | 2 | 未安装 | 两张漫游 SIM 同时在线,收发、通知、断线重连通过;存储 `SM`/`ME` 均为 10。记录早于 JSON 报告格式,细节见 [AT 笔记](at-reference.md) |

历史行保留是为了不丢失已经完成的实测,但它没有机器生成的报告附件。下一次维护窗口应
用下述命令补采,在补采前不把它升级为“恢复”等级。

## 待扩充组合

| 优先级 | 组合 | 当前状态 | 需要补的证据 |
| ---: | --- | --- | --- |
| 1 | Arch rolling + V1011 + 双模块 | 历史端到端 | JSON 报告、冷启动、双模块热插拔和重新编号 |
| 2 | Debian stable x86_64 + V1011 | 仅文档适配 | `dialout` 权限、报告、收发、systemd 重启 |
| 3 | Ubuntu LTS x86_64 + V1011 + ModemManager | 未实测 | ignore 规则生效前后、报告、20 分钟稳定 AT 与收发 |
| 4 | arm64 Linux + V1011 | 仅 Server 镜像可构建 | Agent 串口、报告和端到端收发 |
| 5 | 其他 Air780E AT 固件 | 未实测 | 固件完整 `CGMR`、报告、收发、存储容量和注册恢复 |

## 生成脱敏报告

报告只执行状态与身份查询,不运行 `CMGL`、`CMGR` 或 `CMGD`,因此不会读取或删除
收件箱。它不输出主机名、IMEI、ICCID、SMSC 和运营商名称;USB serial 也只记录
“是否存在”和“是否为 Air780E 通用值”,不保存原值。

串口只能有一个所有者。生产 Agent 运行时不要并发执行探测;在短维护窗口用子 shell
保证探测结束后一定重新启动服务：

```bash
(
  set -e
  trap 'sudo systemctl start air780e-agent' EXIT
  sudo systemctl stop air780e-agent
  sudo -u air780e /opt/air780e-agent/.venv/bin/air780e-probe \
    --report /tmp/air780e-compat-$(date +%F).json
)
```

`validation_ready=true` 且退出码为 0 才表示至少一个 AT 口真实返回了固件版本。即使
失败,JSON 仍会写出:例如只有 sysfs 接口、没有 `/dev/ttyACM*`,或 `CGMR` 不受支持,
都会留下可诊断证据并返回非零。

报告默认可分享,提交前仍应人工检查一次。建议文件名使用
`<日期>-<发行版>-<架构>-<固件>.json`,并与以下人工结果一起记录：

1. 入站短信到达 Agent、Server 和通知渠道;
2. 出站短信被目标号码接收;
3. 冷启动后设备自动上线;
4. 拔插后 `ttyACM` 改号,同一 IMEI/ICCID 的 worker 自动恢复;
5. 若安装 ModemManager,记录 ignore 规则是否生效及持续运行结果。

报告证明查询当时的主机和模块状态,不能替代运营商收发或长稳观察。
