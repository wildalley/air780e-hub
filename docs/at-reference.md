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

#### LTE、IMS 与短信发送边界

Air780E 系列支持短信,但这不等于每个型号和固件都支持 VoLTE,也不等于 LTE 注册后
必然通过 IMS 发送短信。短信还可能由 NAS/SGs 承载,所以 **IMS 未注册只能作为诊断
证据,不能作为拒绝 `AT+CMGS` 的条件**。

2026-08-17 对 `AirM2M_780EPV_V1011_LTE_AT` 的只读查询和唯一一次发送测试显示:

- EPS/LTE 已注册且数据已附着,CS 未注册;
- `AT+CIREG?` 返回 `+CIREG: 2,0`,即固件能报告 IMS 状态但当时未注册;
- `CGSMS`、`IMS`、`IMSREG`、`VOLTE` 等探测命令不受该固件支持;
- `AT+CMGS` 仍被执行,但该次出站失败;同一 SIM 放入手机后可以发送。

Agent 因此只以 best-effort 方式启用和查询 `CIREG`:固件拒绝时状态为“未知”,接受时
在设备页区分 EPS/LTE、CS 与 IMS,并把注册域和完整固件版本附到发送错误。它不会发送
未经该固件文档确认的 `AT+IMS`、`AT+VOLTE` 或 `ECCFG` 配置,也不会把 IMS 未注册
误判成必然不能发短信。

[SimAdmin](https://github.com/3899/SimAdmin) 的发送路径同样没有自行实现 IMS:它调用
ModemManager Messaging 的 `Create` / `Send`,IMS 状态接口当前直接说明 ModemManager
未暴露该状态。[uart_sms_forwarder](https://github.com/dushixiang/uart_sms_forwarder)
则要求烧录 LuatOS,由 `sms.sendLong()` 交给底层固件;其 README 明确提示部分合宙固件
存在收发短信问题。该项目的 [issue #37](https://github.com/dushixiang/uart_sms_forwarder/issues/37)
有 Air780EPV 用户报告 `LuatOS-SoC_V2020_Air780EP` 在不需要 VoLTE 时恢复收发,
[issue #40](https://github.com/dushixiang/uart_sms_forwarder/issues/40) 也有普通 Air780E
刷 V1122 后恢复的个案。这些是固件兼容性线索,不是证明不同型号固件可以互刷的官方
结论;而且切到 LuatOS 后不能继续使用本项目当前的 AT Agent。

上面这段写于只发过一条测试短信的时候。次日的完整排除把成因指向网络侧,固件线索因此
降级为备选,见下一节。

#### 2026-08-18 出站失败的逐项排除

2026-08-17 那次只发了一条就停,证据不足以区分“模块/固件不行”和“网络不放通”。
2026-08-18 用同一张 giffgaff SIM 做了一次完整排除。模块共暴露三个 ACM 口
(`02/04/06`),Agent 只占第一个,所以全程走第三个口旁路复测,Agent 的收发没有中断。

本地成因逐项确认正常:

| 检查 | 结果 |
| --- | --- |
| `AT+CSCA?` 短信中心 | 已设置 |
| `AT+CPIN?` | `READY` |
| `AT+CREG?` / `AT+CGATT?` | `0,1` / `1`,漫游注册并已附着 |
| `AT+COPS?` | 漫游在 `46000` |
| `AT+CSQ` | 26–28,信号良好 |
| `AT+CFUN?` | `1`,射频正常 |
| `AT+CSMS?` | `0,1,1`,MO 与 MT 均支持 |
| `AT+CPMS?` | `"SM",0,10`,存储为空,不是写满 |
| PDU 编码 | 与仓库内 codec 逐字节一致,TP-SRR 置位正确 |

同一模块入站完全正常(库内 11 条),出站在库内 5 次全失败。三次复现给出三种不同症状:
一次 `+CGEV: NW DETACH`(自行恢复)、两次 70 秒内无任何终止码,以及生产日志里的
`+CMS ERROR: unknown error`。**三次对端都没有收到任何内容**,说明消息没有进入网络。

`AT+CEER` 在该固件不受支持(见下),所以拿不到网络侧 cause code,排除法是能做到的
上限。结论:成因在网络侧,该资费的漫游 MO 短信没有放通。固件升级因此不再是首要动作。

复测结束后已恢复原状:第三个口释放,`CMGF` 写回进入时的值,`CMEE` 保持 `1`
(Agent 每次连接都会自己设置)。

#### V1011 的两个固件怪癖(2026-08-18 实测)

同一张 giffgaff SIM 漫游在 46000 上复测,记录两处与手册不一致的行为。两者都已在
Agent 中处理,`mock.py` 也提供 `force_text_errors` 与 `cereg_answers_as_cgreg`
两个开关复现。

**一、`AT+CEREG?` 用 `+CGREG:` 前缀作答。** 连查三次均为同一结果,已排除是插入的 URC:

```
AT+CEREG?  → +CGREG: 0,5
AT+CEREG=? → +CGREG: (0,1,2,3,4,5)
AT+CREG?   → +CREG: 0,1      ← 对照,前缀正常
```

严格按 `+CEREG:` 取值会一直落空,EPS 域因此永远是“未知”,设备页只显示 CS 已注册。
`_read_registration_domain` 现接受多个前缀,把 `+CGREG` 作为 `AT+CEREG?` 的别名。
注意 **`+CGREG` 不能注册成 URC 前缀**:`_handle_line` 会先匹配在途命令的期望前缀,
再走 URC 路由,注册后反而会把这条响应从结果里劫走。EPS 域的推送变化由下一次状态轮询覆盖。

**二、`AT+CMEE=1` 之后错误码仍是文本。** 手册说 `1` 给数字、`2` 给文本,实测两种设置
下都拿不到数字码:

```
CMEE=1(生产日志):AT+CMGS=47 → +CMS ERROR: unknown error         ← 期望 500
CMEE=2(本次实测):AT+CMGR=99 → +CMS ERROR: invalid memory index  ← 期望 321
```

`CMEE=1` 那条来自 2026-08-17 的 Agent 日志(Agent 初始化时确实执行了 `AT+CMEE=1`);
本次裸串口复测中 `CMEE=1` 下同一命令 2 秒内无输出,但那次测试脚本每条命令前会清输入
缓冲,迟到的响应可能被冲掉,所以只以生产日志为据。

数字码丢失的代价是 `no network service`(331)、`requested facility not subscribed`
(50)和 `unknown error`(500)在日志里长得一样,而且 `read_stored` / `delete_stored`
靠 `.code in (321, 322)` 判断“槽位已空”,拿不到码就会误报。客户端现在按 `CMS_ERRORS`
/ `CME_ERRORS` 的规范名称反查出码;表里没有的措辞保持原样上报,不猜。

**`AT+CEER` 不受支持**,返回 `ERROR`。它恰好是唯一能直接问出“`AT+CMGS` 为什么被拒”的
命令,所以该固件上拿不到网络侧 cause code。

### 2.2 信号与网络状态

| 指令 | 拿到什么 |
|---|---|
| `AT+CSQ` | RSSI(0-31)+ 误码率 |
| `AT+CESQ` | 扩展信号质量,LTE 下含 **RSRP / RSRQ** |
| `AT+CCED=<mode>,<dump>` | 小区环境描述;`=0,1` 服务小区,`=0,2` 邻区。**没有裸执行形式** |
| ~~`AT+EEMGINFO`~~ | 工程模式详细网络参数;**V1011 不支持**,五种拼法都返回 `ERROR` |
| `AT+COPS?` | 当前运营商 |
| `AT+CREG?` / `AT+CGREG?` / `AT+CEREG?` | 2G / GPRS / LTE 注册状态 |
| `AT+CIREG?` | IMS 注册状态;部分固件不支持,且未注册不等于 SMS 必然不可用 |
| `AT+CPIN?` | SIM 卡状态 / PIN |
| `AT+ICCID`(或 `AT+CCID`) | 卡 ICCID |
| `AT+CGSN` | IMEI |
| `AT+CGMI` / `AT+CGMM` / `AT+CGMR` | 厂商 / 型号 / 固件版本 |
| `AT+CBC` | 供电电压(**单值毫伏**,见下) |
| `AT+CCLK?` | 模块时钟 |
| `AT+CTZU` / `AT+CTZR` | 时区自动更新 |

> **`AT+CCED` 必须带参数**(2026-08-18 实测,V1011)。裸执行形式返回
> `+CME ERROR: 3`(operation not allowed),读起来像权限问题,实际只是缺参数 ——
> 与 `AT^CACAP` 的 `+CME ERROR: 4`(operation not supported)不是一回事。
>
> ```
> AT+CCED=?    → +CCED: (0,1,2),(1,2,8)
> AT+CCED=0,1  → +CCED:LTE current cell: <13 个字段>
> AT+CCED=0,2  → +CCED:LTE neighbor cell: <每个邻区一行>   (=0,8 返回同样内容)
> AT+CCED=0,3  → +CME ERROR: 50        AT+CCED      → +CME ERROR: 3
> ```
>
> mode 0 是单次读取,mode 2 停止周期上报;Agent 只发 mode 0,不开周期上报。
>
> ⚠️ **服务小区行的第 3 个字段是 IMSI**(实测与 `AT+CIMI` 逐字节一致)。它和设备页
> 已经显示的 ICCID 同级敏感,不要把真实返回粘进公开仓库的文档或测试数据 ——
> mock 里的这一段已经掩码成全 0。
>
> 手册中**未出现 `AT+CIMI`**,但**实测支持**(2026-08-03,V1011 固件):返回裸 IMSI 字符串,无 `+CIMI:` 前缀。`AT+CGSN` 同样返回裸 IMEI。是手册没列全,不是不支持。
>
> 反过来,部分 SIM 的 **`AT+CNUM` 会返回 `ERROR`** —— 号码通常没有写入 SIM,界面上的号码需要人工填写。

> **`AT+CBC` 只返回一个毫伏值**(2026-08-18 实测,两块 V1011 模块均为
> `+CBC: 3968` / `+CBC: 3991`)。GSM 27.007 定义的是三个字段
> `+CBC: <bcs>,<bcl>,<voltage>`,但没有电池的模块没有充电状态可报,固件就只给电压。
>
> ```
> AT+CBC  → +CBC: 3968        (不是 +CBC: 0,80,3968)
> ```
>
> 所以 Agent 的解析**按字段个数分派**而不是按位置取:1 个字段就是电压,3 个字段取第
> 三个,再对结果做 2000–6000 mV 的合理性检查。按位置取的话,某些固件的 `+CBC: 0,80`
> 会被读成 80 mV —— 那不是低电压,那是解析错了。

SimAdmin 仪表盘上的信号、运营商、小区信息基本都能覆盖。拿不到的是**频段锁定、小区锁定**这类操作 —— 那些需要 QMI,EC618 不支持,属于硬件限制而非实现问题。

#### Web 运营商控制与网络诊断

设备详情只封装合宙公开 AT 文档中已有的标准操作:

| Web 操作 | Agent 指令 | 行为 |
|---|---|---|
| 扫描运营商 | `AT+COPS=?` | 返回可见网络的状态、长短名称、MCC/MNC 和接入制式;模块扫描最长可持续数分钟 |
| 手动选择 | `AT+COPS=1,2,"<MCCMNC>"` | 只接受 5 或 6 位数字,避免把任意 AT 文本拼进指令 |
| 恢复自动 | `AT+COPS=0` | 重新交给模块自动选择网络 |
| 网络诊断 | `AT+CCED=0,1`、`AT+CCED=0,2`、`AT+EEMGINFO`、`AT*BANDIND?`、`AT^SYSINFO` | 原样返回各固件的工程参数;其中一条不支持时不丢弃其余结果 |

手动选择生效后,Agent 暂停“自动选择运营商”注册自愈,否则后台会在未注册时发送
`AT+COPS=0` 并撤销管理员选择;恢复自动选网后自愈策略照常运行。运营商选择只决定
尝试注册哪个 PLMN,**不等于锁频或锁小区**,也不能绕过 SIM 的漫游协议、强制 IMS
注册或保证短信发送成功。

当前公开文档没有为本项目所用的 AT 固件确认稳定的锁频、锁小区接口,因此 Web 不会
下发从其它芯片或厂商移植来的私有命令。`AT+COPS=?` 使用 180 秒 Agent 超时和 210 秒
Server 等待时间,不经过普通 Web AT 调试台的 30 秒路径;网络诊断串行读五条指令,
每条 30 秒 AT 超时,Server 等待 165 秒。

##### 锁频/锁小区:V1011 实测确认没有可用接口(2026-08-18)

不是“暂时没做”,是固件没给出能表达“锁到第 N 频段”的写入形式。实测(只发查询与测试
形式,未写入):

```
AT*BANDIND?     → *BANDIND: 0, 39, 7        AT*BANDIND=?   → *BANDIND: (0,1)
AT^SYSCONFIG?   → ^SYSCONFIG: 2,0,1,1       AT^SYSCONFIG=? → (2),(0),(0-2),(1)
AT^SYSINFO      → ^SYSINFO: 2,2,1,17,1,7
AT^CACAP        → +CME ERROR: 4             AT+CPOL?       → ERROR
```

`*BANDIND` 唯一可写形式是 `(0,1)` 的开关,表达不了目标频段;`^SYSCONFIG` 的制式字段
只允许 `(2)`,连接入制式都改不了;`^CACAP` 与 `AT+CPOL`(优先运营商列表)直接不支持。
合宙公开指令索引中也没有锁频、锁小区的**设置**命令 —— `*BANDIND` 与 `AT+CCED` 都只
读不写(`AT+CCED` 的 mode 2 只是停止周期上报,不改变无线状态),`AT+EEMGINFO` 在
V1011 上根本不存在。这与上文“需要 QMI,EC618 不支持”一致。

`AT*BANDIND?` 与 `AT^SYSINFO` 已并入网络诊断,按原始行展示。**字段含义手册未给出**,
以下只是按同类模块惯例的推断,不作为结论:`39` 可能是当前工作频段(Band 39 为 TD-LTE,
与该卡漫游在 `46000` 相符),`7` 与 `AT+COPS` 的接入制式编号一致(E-UTRAN);
`^SYSINFO` 第二个字段 `2` 可能表示仅 PS 域、第三个 `1` 表示漫游中,与实测 CS 未注册
互相印证。因此没有把它们解析成结构化字段,以免把推断固化成 schema。

### 2.3 保号相关

| 指令 | 用途 |
|---|---|
| `AT+CMGS` | 发短信保号 |
| `AT+CIPPING` | **模块自己 ping,消耗流量** |
| `AT+CGACT=0,<cid>` | 停用指定的非 IMS PDP 上下文，关闭用户面数据会话 |
| `AT+CGATT` | 附着/去附着分组服务；`CGATT=0` 可能影响 EPS/IMS 注册，不作为日常“关流量”操作 |
| `ATD<号码>;` | 拨号保号。结尾的 `;` 表示语音呼叫,少了它模块会当数据呼叫 |
| `ATH` | 挂断。无通话时也返回 `OK`,可安全用作清理 |
| `AT+CLCC` | 查询当前通话状态,是"这一通到底有没有出去"的唯一正面证据 |
| `AT+CLIP=1` | 打开来电号码上报,`RING` 之后紧跟一条 `+CLIP` |
| ~~`AT+MNCC`~~ | 文档里的通话控制指令,V1011 未实现。语音保号用标准的 `ATD`/`ATH`/`AT+CLCC` 即可 |

> **实测(2026-08-19,AirM2M_780EPV_V1011,只在空闲 AT 口做只读探测):**
> `ATH` → `OK`、`AT+CLCC` → `OK`、`AT+CLIP=?` → `(0,1)`、`AT+CVHU=?` → `(0,1)`、
> `AT+CRC?` → `+CRC: 0`、`AT+VTS=?` → `(0-9,#,*,A-D),(1-10)`。
> `AT+CMOD?`、`AT+CSTA?`、`AT+CPCMREG?` 都返回 `ERROR`。
> **`ATD` 没有实测** —— 拨出去就是一通真实呼叫,会计费也会真的响铃,所以留给实际使用验证。
>
> ⚠️ **`ATD` 的参数是原样写进 AT 流的**,含 `\r` 的"号码"会提前结束拨号命令、把后面的内容当新命令执行。
> `modem.py` 因此用白名单正则拒绝而不是转义:只放行数字和 GSM 27.007 允许的拨号字符。

> **语音保号的成功判定不能看"有没有抛异常"。** `ATD` 的收尾码 `BUSY`、`NO ANSWER`、
> `NO CARRIER` 在 AT 层全部是 `ATCommandError`,可其中前两个恰恰说明**呼叫已经到达网络、
> 网络代对端回了话** —— 对保号来说这就是成功。所以 `call_keepalive` 把这些码翻译成
> `CallResult.reached_network`,只有"根本没拨出去"(`+CME ERROR`、`ERROR`、串口断开)才继续抛。
> `NO CARRIER` 单独一档:它既可能是对端挂断,也可能是压根没出门,所以不凭它认定成功。

> **保号策略因运营商而异。** 部分运营商单发短信不算活跃,需要通话或流量。建议混合:发短信 + `AT+CIPPING`,必要时加拨号。具体规则按自己两张卡的运营商实测,并在任务配置里做成可调。
>
> ⚠️ **漫游卡先别指望语音保号。** 卡 b(giffgaff 漫游)的 CS 状态本来就不可信 ——
> `+CREG: 0,1` 声称在家庭网络,`+CEREG: 0,5` 却老实报漫游,而短信已经因此发不出去
> (`AT+CMGS` 连一个收尾码都不给)。呼叫走的是同一条 CS/IMS 路径,大概率同样失败,
> 所以这个功能是**按会失败来设计的**:到不了网络就报失败并重试,而不是静悄悄地"保"了个空号。

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
| `AT+CMEE` | 错误码详细程度;Agent 使用 `1` 以求数字 `+CMS` / `+CME` 码。**V1011 无视该设置,始终回文本**,客户端因此按规范名称反查出码,见 [§2.1](#v1011-的两个固件怪癖2026-08-18-实测) |
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
echo 'AT+CMEE=1' | socat - /dev/ttyACMx    # 争取数字 CMS/CME 错误码(V1011 仍回文本)
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
