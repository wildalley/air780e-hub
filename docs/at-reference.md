# AT 指令与硬件笔记

Air780E(EC618)通过 USB 接入 Linux 的实测信息与 AT 指令参考。

指令表提取自合宙官方手册:https://docs.openluat.com/air780e/at/app/at_command

> 本文档中标注「**待验证**」的项目需要结合具体固件和运营商复测。

---

## 1. USB 枚举

Air780E USB 版本插入 Linux 后,内核 `cdc_acm` 驱动直接识别,**无需安装任何驱动**。

参考实测 dmesg(来自 [soxfmr/linux-air780e](https://github.com/soxfmr/linux-air780e)):

```
usb 1-2: New USB device found, idVendor=19d1, idProduct=0001, bcdDevice= 2.00
usb 1-2: Product: EigenComm Compo
usb 1-2: Manufacturer: EigenComm
usb 1-2: SerialNumber: 000000000001
cdc_acm 1-2:1.2: ttyACM3: USB ACM device
cdc_acm 1-2:1.4: ttyACM4: USB ACM device
cdc_acm 1-2:1.6: ttyACM5: USB ACM device
```

- VID:PID = `19d1:0001`
- 枚举出 **3 个** `/dev/ttyACM*`,只有一个是 AT 口,需逐个探测:

```bash
echo 'ATI' | socat - /dev/ttyACM5
# AirM2M_780E_V1171_LTE_AT
# OK
```

**前提:模块跑的必须是 AT 固件。** 合宙板子出厂可能是 LuatOS 固件,那样 USB 出来的是下载/日志口,不吃标准 AT。用 LuaTools 可在两种固件间互刷。到货第一步就是 `dmesg` + `ATI` 确认。

### 1.1 参考枚举结果

VID:PID 与序列号与上面的参考完全一致:

```
1-3   19d1:0001   EigenComm | EigenComm Compo | serial=000000000001
```

**序列号确实是 `000000000001`** —— 决策 D8(按 USB 端口路径绑定而非 `by-id`)的前提坐实了,第二个模块到货必然撞车。

接口布局(`/sys/bus/usb/devices/1-3:1.*`),共 8 个接口:

| 接口 | class/sub/proto | 是什么 |
|---|---|---|
| `1.0` / `1.1` | `e0/01/03` + `0a` | RNDIS 网卡(控制 + 数据),本项目不用 |
| `1.2` / `1.3` | `02/02/01` + `0a` | CDC-ACM #1 |
| `1.4` / `1.5` | `02/02/01` + `0a` | CDC-ACM #2 |
| `1.6` / `1.7` | `02/02/01` + `0a` | CDC-ACM #3 |

三个 ACM 口的**控制接口号是 02 / 04 / 06**,udev 规则里的 `ATTRS{bInterfaceNumber}` 只能取这三个之一(哪个是 AT 口仍待实测)。

**排查提示：没有 `/dev/ttyACM*` 不一定是模块故障。** 如果系统刚升级内核但尚未重启，运行中内核可能找不到对应的 `cdc_acm` 模块：

```bash
uname -r
ls /lib/modules/
modprobe -n -v cdc_acm
```

如果 `uname -r` 与 `/lib/modules/` 中已安装的版本不一致，先重启进入新内核。还应检查 ModemManager 是否占用串口；即使当前未安装，也建议保留 udev 中的 `ID_MM_DEVICE_IGNORE` 规则。

---

## 2. 指令参考

### 2.1 短信

| 指令 | 用途 | 备注 |
|---|---|---|
| `AT+CMGF` | 文本/PDU 模式切换 | **用 PDU 模式**,文本模式中文会出问题 |
| `AT+CSCS` | 字符集 | |
| `AT+CSMP` | 短信参数(编码、有效期) | |
| `AT+CSDH` | 显示详细头信息 | |
| `AT+CPMS` | **选择存储位置** | `SM`=SIM 卡 / `ME`=模块内存 |
| `AT+CNMI` | 新短信与送达回执主动上报(URC) | 使用 `2,1,0,1,0` 接收 `+CMTI` 和 `+CDS` |
| `AT+CNMA` | 新短信确认 | 某些 CNMI 模式下必须回 |
| `AT+CMGL` | 列出存储中的短信 | ⚠️ 见下方存储上限 |
| `AT+CMGR` | 读指定索引 | |
| `AT+CMGD` | 删除短信 | 必须及时调用 |
| `AT+CMGS` | 直接发送 | PDU 设置 TP-SRR；返回的 TP-MR 用于关联送达回执 |
| `AT+CMGW` / `AT+CMSS` | 写入存储 / 从存储发送 | |
| `AT+CMMS` | 多条短信连发(保持链路) | 批量发时省时间 |
| `AT+CSCA` | 短信中心号码 | 发送失败先查这个 |
| `AT+CSMS` | 短信服务选择 | |

#### ⚠️ 存储上限 —— 最重要的坑

`AT+CMGL` 读的是**当前仍存在模块/SIM 存储里的**短信,不是历史记录。SIM 卡通常只存 20~50 条,**存满后新短信可能直接收不到**。

必须的处理流程:

```
+CMTI URC 到达 → AT+CMGR 读出 → 写入自己的数据库 → AT+CMGD 删除
```

真正的"历史短信"在自己库里,模块存储只是临时收件箱。

可尝试 `AT+CPMS="ME","ME","ME"` 切到模块内存(容量通常大于 SIM 卡),但一样要及时清。

> **实测(2026-08-03,AirM2M_780EPV_V1011):`SM` 和 `ME` 都是 10 条**,切 ME 没有任何好处。
>
> ```
> AT+CPMS?                  +CPMS: "SM",0,10,"SM",0,10,"SM",0,10
> AT+CPMS="ME","ME","ME"    +CPMS: 0,10,0,10,0,10
> AT+CPMS=?                 +CPMS: ("ME","SM"),("ME","SM"),("ME","SM")
> ```
>
> 比 PLAN 里假设的 20~50 紧一倍以上。两个后果:一是"收到即读即删"(D7)没有任何妥协余地;二是**主机关机期间的兜底能力只有 10 条**,超出就开始丢新短信 —— 风险表里"主机关机期间不转发"那条的实际窗口比预想小得多。

启动时还需做一次全量 `AT+CMGL` 补读,捞回主机离线期间积压的短信。

### 2.2 信号与网络状态

| 指令 | 拿到什么 |
|---|---|
| `AT+CSQ` | RSSI(0-31)+ 误码率 |
| `AT+CESQ` | 扩展信号质量,LTE 下含 **RSRP / RSRQ** |
| `AT+CCED` | 小区环境描述(服务小区 + 邻区) |
| `AT+EEMGINFO` | 工程模式详细网络参数 |
| `AT+COPS?` | 当前运营商 |
| `AT+CREG?` / `AT+CGREG?` / `AT+CEREG?` | 2G / GPRS / LTE 注册状态 |
| `AT+CPIN?` | SIM 卡状态 / PIN |
| `AT+ICCID`(或 `AT+CCID`) | 卡 ICCID |
| `AT+CGSN` | IMEI |
| `AT+CGMI` / `AT+CGMM` / `AT+CGMR` | 厂商 / 型号 / 固件版本 |
| `AT+CBC` | 电压 |
| `AT+CCLK?` | 模块时钟 |
| `AT+CTZU` / `AT+CTZR` | 时区自动更新 |

> 手册中**未出现 `AT+CIMI`**,但**实测支持**(2026-08-03,V1011 固件):返回裸 IMSI 字符串,无 `+CIMI:` 前缀。`AT+CGSN` 同样返回裸 IMEI。是手册没列全,不是不支持。
>
> 反过来,部分 SIM 的 **`AT+CNUM` 会返回 `ERROR`** —— 号码通常没有写入 SIM,界面上的号码需要人工填写。

SimAdmin 仪表盘上的信号、运营商、小区信息基本都能覆盖。拿不到的是**频段锁定、小区锁定**这类操作 —— 那些需要 QMI,EC618 不支持,属于硬件限制而非实现问题。

### 2.3 保号相关

| 指令 | 用途 |
|---|---|
| `AT+CMGS` | 发短信保号 |
| `AT+CIPPING` | **模块自己 ping,消耗流量** |
| `AT+CGATT` | 附着/去附着 —— 可主动断数据,防保号卡跑流量 |
| `AT+MNCC` | 通话控制。若运营商要求通话才算活跃可能用得上。Air780E 无音频编解码,**估计只能拨通不能通话,待验证** |

> **保号策略因运营商而异。** 部分运营商单发短信不算活跃,需要通话或流量。建议混合:发短信 + `AT+CIPPING`。具体规则到货后按自己两张卡的运营商实测,并在任务配置里做成可调。

### 2.4 模块自带的网络能力(本方案基本用不上,备查)

Air780E 的 AT 固件内置了完整的网络客户端,这意味着**必要时模块可以脱离主机独立推送**:

- **MQTT**:`AT+MCONFIG` / `AT+MCONNECT` / `AT+MPUB` / `AT+MSUB` / `AT+MQTTSTATU`
- **HTTP(S)**:`AT+HTTPINIT` / `AT+HTTPPARA` / `AT+HTTPACTION` / `AT+HTTPREAD` / `AT+HTTPSSL`
- **TCP/UDP**:`AT+CIPSTART` / `AT+CIPSEND` / `AT+CIPCLOSE` / `AT+CIPSSL`
- **FTP**:`AT+FTP*` 一整套
- **TLS**:`AT+SSLCFG`

本方案由主机负责推送(不吃 SIM 流量),这些用不到。但值得记一笔:**如果哪天主机不方便常开,可以退化成模块直连 MQTT 上报**,不用换硬件。

### 2.5 定位(备查)

- `AT+AIRLBS` —— 合宙基站定位服务
- `AT+CIPGSMLOC` —— 基站定位
- `AT+WIFISCAN` / `AT+WIFILOC` —— WiFi 扫描定位
- `AT+CGNS*` —— GNSS 相关。**Air780E 无 GPS**(那是 Air780EG),这组在 780E 上大概率无效

### 2.6 模块控制

| 指令 | 用途 |
|---|---|
| `AT+RESET` | 重启模块 |
| `AT+CPOWD` | 关机 |
| `AT+CFUN` | 射频开关 / 飞行模式 |
| `AT+CSCLK` | 睡眠模式 |
| `AT+SETUSB` | USB 形态配置 |
| `AT+RNDISCALL` | RNDIS 拨号(可当网卡) |
| `AT+CNETLIGHT` | 网络指示灯 |
| `AT+IPR` | 波特率 |
| `AT+CMEE` | **错误码详细程度 —— 初始化时务必设为 `2`**,否则出错只回 `ERROR` 无从排查 |
| `AT+FS*` | 模块文件系统 |
| `AT+CTTS` / `AT+CAUDPLAY` | TTS / 音频播放 |

---

## 3. Linux 侧配置

### 3.1 串口权限

Arch Linux 通常使用 `uucp` 组,Debian / Ubuntu 通常使用 `dialout`;以设备实际属组为准:

```bash
sudo usermod -aG uucp $USER   # 之后需注销重登
ls -l /dev/ttyACM*            # 确认实际属组
```

### 3.2 ModemManager 会抢串口

若系统装了 ModemManager(GNOME/KDE 常自带),它会主动探测 `/dev/ttyACM*` 灌 AT 指令,与 agent 打架 —— 表现为响应错乱或偶发超时。

两个办法,二选一:

```bash
sudo systemctl mask ModemManager
```

或用 udev 规则让它忽略(VID/PID 以自己 dmesg 为准):

```
# /etc/udev/rules.d/99-air780e.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="19d1", ATTR{idProduct}=="0001", ENV{ID_MM_DEVICE_IGNORE}="1"
```

规则文件存在不等于已作用到当前设备。应用规则并重新触发后,用枚举报告检查每个设备的
`modem_manager_ignore_applied`;它直接读取运行中的 udev 数据库。报告汇总的
`modem_manager_safety` 会区分 `not-installed`、`ignore-applied`、
`service-inactive`、`unprotected` 和 `unknown`。

### 3.3 两个模块的设备名固定 —— agent 按 IMEI/ICCID 自己找

`ttyACM*` 编号跟插入顺序走,重启或重插就变。常规做法是按序列号绑 `by-id`,**但上面 dmesg 里 `SerialNumber=000000000001` 是通用值 —— 两个同型号模块序列号完全相同**(本机实测已确认),`by-id` 会直接撞车。

退而求其次是按 USB 物理端口路径绑:

```
SUBSYSTEM=="tty", KERNELS=="1-3", ATTRS{bInterfaceNumber}=="02", SYMLINK+="air780e-a"
```

一个模块可能有多个 CDC-ACM 接口，其中不止一个会响应部分 AT 指令；应通过完整探测和短信 URC 验证实际使用的接口。

但这条路的根本问题是:**它拿"插在哪个孔"当身份**。换个 USB 口就要改规则,而且 udev 永远解决不了"哪个模块是哪个" —— IMEI 在 AT 层,不在 USB 描述符里,udev 根本读不到。

所以 agent 改成**自己找**(取代原决策 D8):启动和每次重连时枚举 `/dev/ttyACM*`,逐个问 `ATI` / `AT+CGSN` / `AT+ICCID`,认领身份匹配的那个口。

```toml
[[devices]]
name = "modem-a"
imei = "000000000000001"      # 示例值;实际部署填模块 IMEI
```

于是:换 USB 口不用改配置,`ttyACM` 重新编号无所谓,USB 复位后模块换个号回来会被自动重新认上。不应答的两个 ACM 口自然被排除,顺带也就不需要猜哪个是 AT 口了。

udev 规则**仍然支持**(配置里写 `port` 就跳过发现),但只在你确实想要固定符号链接时才需要。`ID_MM_DEVICE_IGNORE` 那两条建议留着,防 ModemManager 抢口。

### 3.4 供电

Air780E 发射瞬间电流可达约 2A。插主板前置 USB 口或无源 hub 容易导致模块重启/掉网。**用后置 USB 口或带独立供电的 hub。**

---

## 4. 到货后的验证清单

按顺序走,每步确认再进行下一步:

生产 Agent 运行时先用下述枚举模式;它只读 sysfs/udev,不打开串口：

```bash
air780e-probe --report /tmp/air780e-enumeration.json --enumeration-only
```

它自动按 USB 设备归组 ACM 接口、校验每个 Air780E 的 `02/04/06`,并记录设备节点、
启动会话和实际 udev ignore 属性。需要 `CGMI` / `CGMM` / `CGMR` 时,再按
[兼容性矩阵](compatibility.md#完整-at-报告)进入短维护窗口运行不带
`--enumeration-only` 的完整报告,避免两个进程抢串口。

```bash
# 1. 枚举与固件类型
dmesg | tail -30
#    记下 VID:PID、ttyACM 编号、USB 端口路径

# 2. 找出 AT 口(逐个试)
for p in /dev/ttyACM*; do echo "--- $p"; echo 'ATI' | socat - "$p" ; done
#    期望看到 AirM2M_780E_..._AT。若全无响应 → 是 LuatOS 固件,需刷 AT 固件

# 3. 基础信息
echo 'AT+CMEE=2' | socat - /dev/ttyACMx    # 打开详细错误码
echo 'AT+CPIN?'  | socat - /dev/ttyACMx    # 卡是否就绪
echo 'AT+CSQ'    | socat - /dev/ttyACMx    # 信号
echo 'AT+COPS?'  | socat - /dev/ttyACMx    # 运营商
echo 'AT+CSCA?'  | socat - /dev/ttyACMx    # 短信中心号(发送失败先查这个)
echo 'AT+ICCID'  | socat - /dev/ttyACMx

# 4. 存储容量(填 docs 里的待验证项)
echo 'AT+CPMS?'  | socat - /dev/ttyACMx

# 5. 顺带把 ModemManager 那条路也测了(20 分钟,便宜)
mmcli -L
mmcli -m 0
mmcli -m 0 --messaging-list

# 6. 插上第二个,确认两个都在,并记录各自 USB 端口路径
udevadm info -a /dev/ttyACM3 | grep -m3 KERNELS

# 7. 热插拔观察(命令运行后拔插一个模块;不占串口,可与 Agent 并行)
air780e-probe --report /tmp/air780e-hotplug.json --observe-hotplug 120
```

热插拔报告只证明 USB 拓扑恢复;仍需从 Agent 日志或 Web 设备页确认原 worker 按
IMEI/ICCID 恢复。验证结果回填到[硬件与系统兼容性矩阵](compatibility.md),再更新
本文档的「待验证」项。
