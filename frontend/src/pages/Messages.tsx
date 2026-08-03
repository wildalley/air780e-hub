import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Avatar,
  Box,
  Button,
  Card,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  IconButton,
  InputAdornment,
  List,
  ListItemButton,
  MenuItem,
  Stack,
  TextField,
  Tooltip,
  Typography,
  useMediaQuery,
} from '@mui/material'
import { useTheme } from '@mui/material/styles'
import SendIcon from '@mui/icons-material/SendOutlined'
import SearchIcon from '@mui/icons-material/SearchOutlined'
import ArrowBackIcon from '@mui/icons-material/ArrowBackOutlined'
import EditIcon from '@mui/icons-material/EditOutlined'
import ErrorIcon from '@mui/icons-material/ErrorOutline'
import RefreshIcon from '@mui/icons-material/RefreshOutlined'
import { api, ApiError, type Conversation, type Device, type Message } from '../api'
import { Loading, useToast } from '../components/common'
import { STATUS } from '../tokens'

/**
 * Messages, as a messaging app.
 *
 * A table of rows is the wrong shape for SMS: what you actually want to know
 * is "what did 10086 say", and a table makes you reassemble that yourself.
 * So: threads on the left, a conversation on the right, replies composed in
 * place rather than in a modal that makes you retype the number.
 *
 * Bubbles are aligned *and* coloured *and* labelled by direction — alignment
 * alone disappears for a screen-reader, colour alone disappears under CVD.
 */

const THREAD_PAGE = 200
// Roughly where a GSM-7 message splits.  Only a hint: the agent does the real
// segmentation, and Unicode content splits far earlier.
const SINGLE_SEGMENT = 70

export function MessagesPage() {
  const toast = useToast()
  const theme = useTheme()
  const narrow = useMediaQuery(theme.breakpoints.down('md'))

  const [threads, setThreads] = useState<Conversation[] | null>(null)
  const [selected, setSelected] = useState<Conversation | null>(null)
  const [search, setSearch] = useState('')
  const [devices, setDevices] = useState<Device[]>([])
  const [composeOpen, setComposeOpen] = useState(false)

  const loadThreads = useCallback(async () => {
    const rows = await api.messages.conversations()
    setThreads(rows)
    return rows
  }, [])

  useEffect(() => {
    void loadThreads()
    void api.devices.list().then(setDevices)
  }, [loadThreads])

  // Keep the open thread pointed at the freshest row, so its preview and
  // count stay honest after a reply is sent.
  useEffect(() => {
    if (!selected || !threads) return
    const fresh = threads.find(
      (t) => t.peer === selected.peer && t.sim_id === selected.sim_id,
    )
    if (fresh && fresh.last_id !== selected.last_id) setSelected(fresh)
  }, [threads, selected])

  const filtered = useMemo(() => {
    if (!threads) return null
    const needle = search.trim().toLowerCase()
    if (!needle) return threads
    return threads.filter(
      (t) =>
        t.peer.toLowerCase().includes(needle) ||
        t.last_body.toLowerCase().includes(needle) ||
        (t.sim_label ?? '').toLowerCase().includes(needle),
    )
  }, [threads, search])

  const showList = !narrow || selected === null
  const showThread = !narrow || selected !== null

  return (
    <Stack spacing={3} sx={{ height: '100%' }}>
      <Stack direction="row" alignItems="center" spacing={2}>
        <Typography variant="h1" sx={{ flexGrow: 1 }}>
          短信
        </Typography>
        <Tooltip title="刷新">
          <IconButton onClick={() => void loadThreads()} size="small">
            <RefreshIcon />
          </IconButton>
        </Tooltip>
        <Button
          variant="contained"
          startIcon={<EditIcon />}
          onClick={() => setComposeOpen(true)}
          disabled={devices.length === 0}
        >
          新会话
        </Button>
      </Stack>

      <Card
        sx={{
          display: 'flex',
          flexGrow: 1,
          minHeight: 0,
          height: { xs: 'calc(100vh - 200px)', md: 'calc(100vh - 220px)' },
          overflow: 'hidden',
        }}
      >
        {showList && (
          <ThreadList
            threads={filtered}
            selected={selected}
            onSelect={setSelected}
            search={search}
            onSearch={setSearch}
            fullWidth={narrow}
          />
        )}
        {showList && showThread && <Divider orientation="vertical" flexItem />}
        {showThread && (
          <ThreadView
            thread={selected}
            devices={devices}
            onBack={narrow ? () => setSelected(null) : undefined}
            onSent={async () => {
              toast.show('已发送', 'success')
              await loadThreads()
            }}
            onError={(msg) => toast.show(msg, 'error')}
          />
        )}
      </Card>

      <ComposeDialog
        open={composeOpen}
        devices={devices}
        onClose={() => setComposeOpen(false)}
        onSent={async (peer) => {
          setComposeOpen(false)
          toast.show('已发送', 'success')
          const rows = await loadThreads()
          const thread = rows.find((t) => t.peer === peer)
          if (thread) setSelected(thread)
        }}
        onError={(msg) => toast.show(msg, 'error')}
      />
      {toast.element}
    </Stack>
  )
}

// --------------------------------------------------------------------------
// thread list
// --------------------------------------------------------------------------

function ThreadList({
  threads,
  selected,
  onSelect,
  search,
  onSearch,
  fullWidth,
}: {
  threads: Conversation[] | null
  selected: Conversation | null
  onSelect: (thread: Conversation) => void
  search: string
  onSearch: (value: string) => void
  fullWidth: boolean
}) {
  return (
    <Stack sx={{ width: fullWidth ? '100%' : 340, flexShrink: 0, minHeight: 0 }}>
      <Box sx={{ p: 1.5 }}>
        <TextField
          size="small"
          fullWidth
          placeholder="搜索号码或内容"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon fontSize="small" />
              </InputAdornment>
            ),
          }}
        />
      </Box>
      <Divider />
      <Box sx={{ overflowY: 'auto', flexGrow: 1 }}>
        {threads === null ? (
          <Loading />
        ) : threads.length === 0 ? (
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{ p: 4, textAlign: 'center' }}
          >
            还没有短信
          </Typography>
        ) : (
          <List disablePadding>
            {threads.map((thread) => {
              const active =
                selected?.peer === thread.peer && selected?.sim_id === thread.sim_id
              return (
                <ListItemButton
                  key={`${thread.sim_id}-${thread.peer}`}
                  selected={active}
                  onClick={() => onSelect(thread)}
                  sx={{ alignItems: 'flex-start', py: 1.25, gap: 1.5 }}
                >
                  <PeerAvatar peer={thread.peer} />
                  <Box sx={{ minWidth: 0, flexGrow: 1 }}>
                    <Stack direction="row" alignItems="baseline" spacing={1}>
                      <Typography
                        variant="subtitle2"
                        noWrap
                        sx={{ flexGrow: 1, fontWeight: 600 }}
                      >
                        {thread.peer || '(未知号码)'}
                      </Typography>
                      <Typography
                        variant="caption"
                        color="text.secondary"
                        sx={{ flexShrink: 0 }}
                      >
                        {shortTime(thread.last_ts)}
                      </Typography>
                    </Stack>
                    <Typography
                      variant="body2"
                      color="text.secondary"
                      noWrap
                      sx={{ mt: 0.25 }}
                    >
                      {thread.last_direction === 'out' && '你:'}
                      {thread.last_body || '(空)'}
                    </Typography>
                    <Stack direction="row" spacing={0.75} sx={{ mt: 0.75 }}>
                      <Chip
                        size="small"
                        variant="outlined"
                        label={thread.sim_label || thread.sim_iccid?.slice(-6) || thread.device}
                        sx={{ height: 20, fontSize: '0.7rem' }}
                      />
                      {thread.message_count > 1 && (
                        <Chip
                          size="small"
                          variant="outlined"
                          label={`${thread.message_count} 条`}
                          sx={{ height: 20, fontSize: '0.7rem' }}
                        />
                      )}
                    </Stack>
                  </Box>
                </ListItemButton>
              )
            })}
          </List>
        )}
      </Box>
    </Stack>
  )
}

function PeerAvatar({ peer }: { peer: string }) {
  // Service numbers are the common case and read better as digits than as an
  // initial, so show the tail rather than a letter.
  const label = /^\+?\d+$/.test(peer) ? peer.slice(-2) : peer.slice(0, 1).toUpperCase()
  return (
    <Avatar sx={{ width: 36, height: 36, fontSize: '0.85rem', mt: 0.25 }}>
      {label || '?'}
    </Avatar>
  )
}

// --------------------------------------------------------------------------
// one conversation
// --------------------------------------------------------------------------

function ThreadView({
  thread,
  devices,
  onBack,
  onSent,
  onError,
}: {
  thread: Conversation | null
  devices: Device[]
  onBack?: () => void
  onSent: () => void | Promise<void>
  onError: (message: string) => void
}) {
  const [messages, setMessages] = useState<Message[] | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const bottom = useRef<HTMLDivElement | null>(null)

  const load = useCallback(async () => {
    if (!thread) return
    const data = await api.messages.list({
      peer: thread.peer,
      sim_id: thread.sim_id ?? undefined,
      limit: THREAD_PAGE,
    })
    // The API returns newest first; a conversation reads oldest first.
    setMessages([...data.items].reverse())
  }, [thread])

  useEffect(() => {
    setMessages(null)
    setDraft('')
    void load()
  }, [load])

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [messages])

  if (!thread) {
    return (
      <Stack
        sx={{ flexGrow: 1, alignItems: 'center', justifyContent: 'center', p: 4 }}
        spacing={1}
      >
        <Typography variant="body1" color="text.secondary">
          选择左边的一个会话
        </Typography>
        <Typography variant="caption" color="text.secondary">
          或者点右上角「新会话」给一个新号码发短信
        </Typography>
      </Stack>
    )
  }

  // Reply from the card the thread belongs to — replying from the other one
  // would reach the correspondent from a number they do not recognise.
  const device = devices.find(
    (d) => d.iccid && thread.sim_iccid && d.iccid === thread.sim_iccid,
  ) ?? devices.find((d) => d.name === thread.device)
  const online = Boolean(device?.online)

  const send = async () => {
    if (!device) return
    setBusy(true)
    try {
      await api.messages.send(device.name, thread.peer, draft)
      setDraft('')
      await load()
      await onSent()
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '发送失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Stack sx={{ flexGrow: 1, minWidth: 0, minHeight: 0 }}>
      <Stack
        direction="row"
        alignItems="center"
        spacing={1.5}
        sx={{ p: 1.5, flexShrink: 0 }}
      >
        {onBack && (
          <IconButton onClick={onBack} size="small" aria-label="返回会话列表">
            <ArrowBackIcon />
          </IconButton>
        )}
        <PeerAvatar peer={thread.peer} />
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="h3" noWrap>
            {thread.peer || '(未知号码)'}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {thread.sim_label || thread.sim_iccid || thread.device}
            {device && !online && ' · 模块离线'}
          </Typography>
        </Box>
      </Stack>
      <Divider />

      <Box sx={{ flexGrow: 1, overflowY: 'auto', px: 2, py: 2, minHeight: 0 }}>
        {messages === null ? (
          <Loading />
        ) : (
          <Stack spacing={0.5}>
            {messages.map((message, index) => (
              <Bubble
                key={message.id}
                message={message}
                showDay={index === 0 || !sameDay(messages[index - 1].ts, message.ts)}
              />
            ))}
            <div ref={bottom} />
          </Stack>
        )}
      </Box>

      <Divider />
      <Box sx={{ p: 1.5, flexShrink: 0 }}>
        {!device ? (
          <Alert severity="warning" variant="outlined">
            这张卡当前不在任何在线模块上,无法回复
          </Alert>
        ) : (
          <Stack direction="row" spacing={1} alignItems="flex-end">
            <TextField
              fullWidth
              size="small"
              multiline
              maxRows={5}
              placeholder={online ? `回复 ${thread.peer}` : '模块离线,暂时发不出去'}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              disabled={!online || busy}
              onKeyDown={(e) => {
                // Enter sends, Shift+Enter makes a new line — the convention
                // every messaging app has trained people on.
                if (e.key === 'Enter' && !e.shiftKey && draft.trim() && !busy) {
                  e.preventDefault()
                  void send()
                }
              }}
              helperText={
                draft.length > SINGLE_SEGMENT
                  ? `${draft.length} 字 · 会拆成多段发送`
                  : draft.length > 0
                    ? `${draft.length} 字`
                    : ' '
              }
            />
            <IconButton
              color="primary"
              onClick={() => void send()}
              disabled={!online || busy || !draft.trim()}
              aria-label="发送"
              sx={{ mb: 2.5 }}
            >
              <SendIcon />
            </IconButton>
          </Stack>
        )}
      </Box>
    </Stack>
  )
}

function Bubble({ message, showDay }: { message: Message; showDay: boolean }) {
  const outgoing = message.direction === 'out'
  const failed = message.status === 'failed'

  return (
    <>
      {showDay && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 1.5 }}>
          <Chip
            size="small"
            label={dayLabel(message.ts)}
            sx={{ height: 22, fontSize: '0.72rem' }}
          />
        </Box>
      )}
      <Box
        sx={{
          display: 'flex',
          justifyContent: outgoing ? 'flex-end' : 'flex-start',
          pb: 0.5,
        }}
      >
        <Box sx={{ maxWidth: { xs: '85%', sm: '72%' }, minWidth: 0 }}>
          <Box
            sx={{
              px: 1.75,
              py: 1.25,
              borderRadius: 2.5,
              // The corner nearest the sender is squared off — the visual
              // "tail" that says which side a message came from.
              borderBottomRightRadius: outgoing ? 6 : undefined,
              borderBottomLeftRadius: outgoing ? undefined : 6,
              bgcolor: outgoing ? 'primary.main' : 'action.hover',
              color: outgoing ? 'primary.contrastText' : 'text.primary',
              border: failed ? `1px solid ${STATUS.critical}` : undefined,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            <Typography variant="body2">{message.body}</Typography>
          </Box>
          <Stack
            direction="row"
            spacing={0.75}
            alignItems="center"
            justifyContent={outgoing ? 'flex-end' : 'flex-start'}
            sx={{ mt: 0.4, px: 0.5 }}
          >
            {/* Spelled out, not just implied by which side it sits on. */}
            <Typography variant="caption" color="text.secondary">
              {outgoing ? '发出' : '收到'} {clockTime(message.ts)}
            </Typography>
            {message.segments > 1 && (
              <Typography variant="caption" color="text.secondary">
                · {message.segments} 段
              </Typography>
            )}
            {failed && (
              <Tooltip title={message.error ?? '未知原因'}>
                <Stack direction="row" spacing={0.25} alignItems="center">
                  <ErrorIcon sx={{ fontSize: 14, color: STATUS.critical }} />
                  <Typography variant="caption" sx={{ color: STATUS.critical }}>
                    发送失败
                  </Typography>
                </Stack>
              </Tooltip>
            )}
          </Stack>
        </Box>
      </Box>
    </>
  )
}

// --------------------------------------------------------------------------
// new conversation
// --------------------------------------------------------------------------

function ComposeDialog({
  open,
  devices,
  onClose,
  onSent,
  onError,
}: {
  open: boolean
  devices: Device[]
  onClose: () => void
  onSent: (peer: string) => void | Promise<void>
  onError: (message: string) => void
}) {
  const [device, setDevice] = useState('')
  const [number, setNumber] = useState('')
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open && !device && devices.length) setDevice(devices[0].name)
  }, [open, device, devices])

  const send = async () => {
    setBusy(true)
    try {
      await api.messages.send(device, number, body)
      const peer = number
      setNumber('')
      setBody('')
      await onSent(peer)
    } catch (err) {
      // The agent's own error text (a +CMS code, "device offline") is far more
      // actionable than a generic failure.
      onError(err instanceof ApiError ? err.message : '发送失败')
    } finally {
      setBusy(false)
    }
  }

  const online = devices.find((d) => d.name === device)?.online

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>新会话</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            select
            label="使用哪张卡"
            value={device}
            onChange={(e) => setDevice(e.target.value)}
            fullWidth
          >
            {devices.map((d) => (
              <MenuItem key={d.name} value={d.name} disabled={!d.online}>
                {d.sim_label || d.label || d.name}
                {d.online ? '' : '(离线)'}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="目标号码"
            value={number}
            onChange={(e) => setNumber(e.target.value)}
            placeholder="10086"
            fullWidth
          />
          <TextField
            label="内容"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            multiline
            minRows={3}
            fullWidth
            helperText={`${body.length} 字${body.length > SINGLE_SEGMENT ? ' · 将拆成多段发送' : ''}`}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>取消</Button>
        <Button
          variant="contained"
          onClick={() => void send()}
          disabled={busy || !device || !number || !body || !online}
        >
          发送
        </Button>
      </DialogActions>
    </Dialog>
  )
}

// --------------------------------------------------------------------------
// time helpers — a thread wants clock times, not full stamps
// --------------------------------------------------------------------------

const pad = (n: number) => String(n).padStart(2, '0')

function clockTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function sameDay(a: string, b: string): boolean {
  const x = new Date(a)
  const y = new Date(b)
  return (
    x.getFullYear() === y.getFullYear() &&
    x.getMonth() === y.getMonth() &&
    x.getDate() === y.getDate()
  )
}

function dayLabel(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const today = new Date()
  const yesterday = new Date(today)
  yesterday.setDate(today.getDate() - 1)
  if (sameDay(d.toISOString(), today.toISOString())) return '今天'
  if (sameDay(d.toISOString(), yesterday.toISOString())) return '昨天'
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** List column: a time for today, a date for anything older. */
function shortTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ''
  if (sameDay(d.toISOString(), new Date().toISOString())) return clockTime(ts)
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
