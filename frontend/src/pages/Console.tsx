import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  IconButton,
  MenuItem,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import { useTheme } from '@mui/material/styles'
import SendIcon from '@mui/icons-material/SendOutlined'
import ClearIcon from '@mui/icons-material/DeleteSweepOutlined'
import { api, ApiError, type Device } from '../api'
import { Loading } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { STATUS, VIZ } from '../tokens'

/**
 * Web AT 调试控制台.
 *
 * 直接对某个模块下发原始 AT 指令,看它回了什么 —— 排查发送失败、确认卡状态、
 * 读工程参数时最快的一条路.  走的是和短信发送同一条链路(`POST /api/at`):
 * 服务器把指令转给认领了这个设备的 agent,agent 串到模块,把每一行回显带回来.
 *
 * 一次请求换一次完整回复 —— 模块跑完指令才应答,不是逐字符流式.  所以每条
 * 指令先以「执行中」占位,拿到结果再补上回显.  指令成功(最终 `OK`)只返回
 * 信息行,末尾的 `OK` 由前端补一个绿色标记;失败时 502 带回模块自己的错误
 * 文本(`+CME ERROR: …` / `ERROR` / 「设备离线」),原样显示成红色.
 *
 * 终端配色不靠颜色单独承载状态:成功/失败都另有文字标签,CVD 与强制配色下
 * 依然读得出.
 */

const HISTORY_KEY = 'hub.at-history'
const HISTORY_MAX = 100
// RawAtBody 后端限制:2–200 字符.
const CMD_MIN = 2
const CMD_MAX = 200

const MONO =
  '"SFMono-Regular", ui-monospace, Consolas, "Liberation Mono", Menlo, monospace'

// 常用只读查询,点一下直接下发.  取自 docs/at-reference.md 里到货后实测过的那批.
const QUICK: { cmd: string; hint: string }[] = [
  { cmd: 'ATI', hint: '模块信息' },
  { cmd: 'AT+CSQ', hint: '信号' },
  { cmd: 'AT+CESQ', hint: 'RSRP/RSRQ' },
  { cmd: 'AT+COPS?', hint: '运营商' },
  { cmd: 'AT+CEREG?', hint: 'LTE 注册' },
  { cmd: 'AT+CPIN?', hint: 'SIM 状态' },
  { cmd: 'AT+ICCID', hint: 'ICCID' },
  { cmd: 'AT+CGSN', hint: 'IMEI' },
  { cmd: 'AT+CPMS?', hint: '短信存储' },
  { cmd: 'AT+CSCA?', hint: '短信中心' },
  { cmd: 'AT+CGATT?', hint: '数据附着' },
  { cmd: 'AT+CCLK?', hint: '模块时钟' },
]

// 会重启模块 / 关机 / 关射频的指令,下发前问一句 —— 手滑一个 AT+RESET 就得
// 等模块重新枚举、重连、重新认领,代价不小.
const DESTRUCTIVE = /^AT\+(RESET|CPOWD|CFUN=0|CRESET)\b/i

interface Entry {
  id: number
  device: string
  command: string
  ts: number
  status: 'pending' | 'ok' | 'error'
  lines: string[]
  error?: string
}

function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === 'string') : []
  } catch {
    return []
  }
}

export function ConsolePage() {
  const theme = useTheme()
  const viz = VIZ[theme.palette.mode]

  const [devices, setDevices] = useState<Device[] | null>(null)
  const [device, setDevice] = useState('')
  const [entries, setEntries] = useState<Entry[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState<string | null>(null)

  // 命令历史:上下方向键回放,跨会话留存.  `cursor` 为 -1 表示停在正在输入的
  // 草稿上,>=0 表示落在历史里的某一条.
  const history = useRef<string[]>(loadHistory())
  const [cursor, setCursor] = useState(-1)
  const nextId = useRef(1)
  const bottom = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    void api.devices.list().then((rows) => {
      setDevices(rows)
      // 默认选中第一个在线模块;都不在线就退回列表第一个,让用户至少能看清选了谁.
      const online = rows.find((d) => d.online)
      setDevice((online ?? rows[0])?.name ?? '')
    })
  }, [])

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [entries])

  const rememberHistory = useCallback((command: string) => {
    // 连续重复的同一条只留一份,历史列表不至于被 CSQ 刷屏.
    const list = history.current
    if (list[list.length - 1] !== command) {
      list.push(command)
      if (list.length > HISTORY_MAX) list.splice(0, list.length - HISTORY_MAX)
      try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(list))
      } catch {
        /* localStorage 满了或被禁用 —— 历史丢就丢,不影响下发 */
      }
    }
  }, [])

  const run = useCallback(
    async (raw: string) => {
      const command = raw.trim()
      if (!device || command.length < CMD_MIN || busy) return

      rememberHistory(command)
      setCursor(-1)
      setDraft('')

      const id = nextId.current++
      setEntries((prev) => [
        ...prev,
        { id, device, command, ts: Date.now(), status: 'pending', lines: [] },
      ])
      setBusy(true)
      try {
        const { lines } = await api.at(device, command)
        setEntries((prev) =>
          prev.map((e) => (e.id === id ? { ...e, status: 'ok', lines } : e)),
        )
      } catch (err) {
        // 模块自己的错误文本(+CME/+CMS 码、ERROR、设备离线)比通用失败有用得多.
        const message = err instanceof ApiError ? err.message : '下发失败'
        setEntries((prev) =>
          prev.map((e) => (e.id === id ? { ...e, status: 'error', error: message } : e)),
        )
      } finally {
        setBusy(false)
        // 焦点交还输入框,连着敲下一条不用再点一次.
        inputRef.current?.focus()
      }
    },
    [device, busy, rememberHistory],
  )

  const submit = useCallback(
    (command: string) => {
      const trimmed = command.trim()
      if (DESTRUCTIVE.test(trimmed)) {
        setConfirm(trimmed)
        return
      }
      void run(trimmed)
    },
    [run],
  )

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit(draft)
      return
    }
    // 上/下方向键在历史里翻,行为对齐大多数终端.
    const list = history.current
    if (event.key === 'ArrowUp' && list.length) {
      event.preventDefault()
      const at = cursor === -1 ? list.length - 1 : Math.max(0, cursor - 1)
      setCursor(at)
      setDraft(list[at])
    } else if (event.key === 'ArrowDown' && cursor !== -1) {
      event.preventDefault()
      const at = cursor + 1
      if (at >= list.length) {
        setCursor(-1)
        setDraft('')
      } else {
        setCursor(at)
        setDraft(list[at])
      }
    }
  }

  const selected = useMemo(
    () => devices?.find((d) => d.name === device),
    [devices, device],
  )
  const online = Boolean(selected?.online)

  if (devices === null) return <Loading />

  const tooShort = draft.trim().length > 0 && draft.trim().length < CMD_MIN

  return (
    <Stack spacing={3} sx={{ height: '100%' }}>
      <PageHeader
        title="AT 调试"
        subtitle="直连模块,下发原始 AT 指令看回显"
        actions={
          <>
            <TextField
              select
              size="small"
              label="模块"
              value={device}
              onChange={(e) => setDevice(e.target.value)}
              sx={{ minWidth: 200 }}
              disabled={devices.length === 0}
            >
              {devices.map((d) => (
                <MenuItem key={d.name} value={d.name}>
                  {d.sim_label || d.label || d.name}
                  {d.online ? '' : '(离线)'}
                </MenuItem>
              ))}
            </TextField>
            <Tooltip title="清空控制台">
              <span>
                <IconButton
                  onClick={() => setEntries([])}
                  disabled={entries.length === 0}
                  aria-label="清空控制台"
                >
                  <ClearIcon />
                </IconButton>
              </span>
            </Tooltip>
          </>
        }
      />

      {devices.length === 0 ? (
        <Alert severity="info" variant="outlined">
          还没有任何模块接入,连上 agent 后再来.
        </Alert>
      ) : (
        <>
          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
            {QUICK.map((q) => (
              <Tooltip key={q.cmd} title={q.hint}>
                <Chip
                  label={q.cmd}
                  size="small"
                  variant="outlined"
                  onClick={() => submit(q.cmd)}
                  disabled={busy || !device}
                  sx={{ fontFamily: MONO, fontSize: '0.75rem' }}
                />
              </Tooltip>
            ))}
          </Stack>

          {selected && !online && (
            <Alert severity="warning" variant="outlined">
              「{selected.sim_label || selected.label || selected.name}」当前离线,指令多半会以「设备离线」返回.
            </Alert>
          )}

          <Box
            sx={{
              flexGrow: 1,
              minHeight: 0,
              overflowY: 'auto',
              p: 2,
              borderRadius: 2,
              border: `1px solid ${viz.border}`,
              bgcolor: viz.plane,
              fontFamily: MONO,
              fontSize: '0.82rem',
              lineHeight: 1.55,
              height: { xs: 'calc(100vh - 360px)', md: 'calc(100vh - 340px)' },
            }}
          >
            {entries.length === 0 ? (
              <Typography
                sx={{ color: viz.muted, fontFamily: MONO, fontSize: '0.82rem' }}
              >
                在下面输入 AT 指令,或点上面的常用查询.回车下发,↑/↓ 翻历史.
              </Typography>
            ) : (
              <Stack spacing={1.5}>
                {entries.map((entry) => (
                  <ConsoleEntry key={entry.id} entry={entry} viz={viz} />
                ))}
              </Stack>
            )}
            <div ref={bottom} />
          </Box>

          <Stack direction="row" spacing={1} alignItems="flex-start">
            <TextField
              fullWidth
              size="small"
              inputRef={inputRef}
              placeholder={device ? 'AT+…' : '先选一个模块'}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value.slice(0, CMD_MAX))
                setCursor(-1)
              }}
              onKeyDown={onKeyDown}
              disabled={!device || busy}
              error={tooShort}
              helperText={
                tooShort
                  ? `至少 ${CMD_MIN} 个字符`
                  : draft.length >= CMD_MAX
                    ? `已到 ${CMD_MAX} 字符上限`
                    : ' '
              }
              InputProps={{ sx: { fontFamily: MONO } }}
            />
            <Button
              variant="contained"
              onClick={() => submit(draft)}
              disabled={!device || busy || draft.trim().length < CMD_MIN}
              startIcon={<SendIcon />}
              sx={{ flexShrink: 0, mt: 0.25 }}
            >
              下发
            </Button>
          </Stack>
        </>
      )}

      <Dialog open={confirm !== null} onClose={() => setConfirm(null)}>
        <DialogTitle>确认下发?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            <Box component="span" sx={{ fontFamily: MONO }}>
              {confirm}
            </Box>{' '}
            会重启或关闭模块,之后要等它重新枚举、被 agent 重新认领才能继续用.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirm(null)}>取消</Button>
          <Button
            color="warning"
            variant="contained"
            onClick={() => {
              const cmd = confirm
              setConfirm(null)
              if (cmd) void run(cmd)
            }}
          >
            仍然下发
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  )
}

function ConsoleEntry({
  entry,
  viz,
}: {
  entry: Entry
  viz: (typeof VIZ)['light']
}) {
  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="baseline">
        <Box component="span" sx={{ color: viz.series[0], userSelect: 'none' }}>
          &gt;
        </Box>
        <Box component="span" sx={{ color: viz.textPrimary, wordBreak: 'break-all' }}>
          {entry.command}
        </Box>
        <Box component="span" sx={{ color: viz.muted, fontSize: '0.72rem', ml: 'auto' }}>
          {clock(entry.ts)}
        </Box>
      </Stack>
      {entry.status === 'pending' ? (
        <Box sx={{ color: viz.muted, pl: 2 }}>执行中…</Box>
      ) : entry.status === 'error' ? (
        <Box sx={{ color: STATUS.critical, pl: 2, whiteSpace: 'pre-wrap' }}>
          {entry.error}
        </Box>
      ) : (
        <Box sx={{ pl: 2 }}>
          {entry.lines.map((line, i) => (
            <Box
              key={i}
              sx={{ color: viz.textSecondary, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}
            >
              {line}
            </Box>
          ))}
          {/* 后端只回信息行,最终的 OK 由前端补 —— 空回复的成功也得看得出来. */}
          <Box sx={{ color: STATUS.good }}>OK</Box>
        </Box>
      )}
    </Box>
  )
}

const pad = (n: number) => String(n).padStart(2, '0')

function clock(ts: number): string {
  const d = new Date(ts)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
