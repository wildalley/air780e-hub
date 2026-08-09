import { useEffect, useMemo, useRef, useState, useCallback, type ReactNode } from 'react'
import useSWR from 'swr'
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
import ErrorIcon from '@mui/icons-material/ErrorOutlined'
import RefreshIcon from '@mui/icons-material/RefreshOutlined'
import DownloadIcon from '@mui/icons-material/FileDownloadOutlined'
import ContentCopyIcon from '@mui/icons-material/ContentCopyOutlined'
import DataIcon from '@mui/icons-material/DataObjectOutlined'
import CheckIcon from '@mui/icons-material/CheckOutlined'
import { detectOtp, hasOlderMessages, OTP_RE, resolveThread, threadPreview } from '../messages'
import { api, ApiError, type Conversation, type Device, type Message } from '../api'
import { useToast } from '../toast'
import { Loading } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { useDebounced } from '../swr'
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

/** Split a body around its codes; codes render as highlighted tokens. */
function highlightOtp(body: string): ReactNode[] {
  const parts: ReactNode[] = []
  let last = 0
  for (const match of body.matchAll(OTP_RE)) {
    if (match.index > last) parts.push(body.slice(last, match.index))
    parts.push(
      <Box
        component="span"
        key={match.index}
        sx={{
          px: 0.5,
          py: 0.1,
          mx: 0.25,
          borderRadius: 0.75,
          bgcolor: 'primary.main',
          color: 'primary.contrastText',
          fontVariantNumeric: 'tabular-nums',
          letterSpacing: '0.08em',
          fontWeight: 700,
        }}
      >
        {match[1]}
      </Box>,
    )
    last = match.index + match[1].length
  }
  if (last < body.length) parts.push(body.slice(last))
  return parts
}

export function MessagesPage() {
  const toast = useToast()
  const theme = useTheme()
  const narrow = useMediaQuery(theme.breakpoints.down('md'))

  // Only the identity of the open thread is state; its contents are read back
  // out of the thread list below, so a reply cannot leave a stale preview
  // pinned to the header.
  const [openThread, setOpenThread] = useState<Pick<
    Conversation,
    'peer' | 'sim_id'
  > | null>(null)
  const [fallback, setFallback] = useState<Conversation | null>(null)
  const [search, setSearch] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [composeOpen, setComposeOpen] = useState(false)

  const { data: threads, mutate: loadThreads } = useSWR(
    '/api/messages/conversations',
    () => api.messages.conversations(),
    { refreshInterval: 10_000 }
  )
  const { data: devices = [] } = useSWR('/api/devices', () => api.devices.list(), {
    refreshInterval: 30_000,
  })

  const setSelected = useCallback((thread: Conversation | null) => {
    setOpenThread(thread && { peer: thread.peer, sim_id: thread.sim_id })
    // A thread opened from search may not be in the list yet (older than the
    // conversations window) — keep the row itself as the fallback.
    setFallback(thread)
  }, [])

  const selected = resolveThread(openThread, threads, fallback)

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
    <Stack spacing={3} sx={{ flexGrow: 1, minHeight: 0 }}>
      <PageHeader
        title="短信"
        subtitle="会话视图,回复在对话里完成"
        actions={
          <>
            <Tooltip title="搜索全部短信">
              <IconButton onClick={() => setSearchOpen(true)} size="small">
                <SearchIcon />
              </IconButton>
            </Tooltip>
            <Tooltip title="导出 CSV">
              <IconButton onClick={() => void api.messages.exportCsv()} size="small">
                <DownloadIcon />
              </IconButton>
            </Tooltip>
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
          </>
        }
      />

      <Card
        sx={{
          display: 'flex',
          flexGrow: 1,
          minHeight: 0,
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
            key={selected ? `${selected.sim_id}:${selected.peer}` : 'empty'}
            thread={selected}
            devices={devices}
            onBack={narrow ? () => setSelected(null) : undefined}
            onRead={() => void loadThreads()}
            onSent={async () => {
              toast.show('已发送', 'success')
              await loadThreads()
            }}
            onError={(msg) => toast.show(msg, 'error')}
          />
        )}
      </Card>

      {/* Mounted only while open, so each visit starts with an empty query —
          the reset an effect used to do. */}
      {searchOpen && (
        <SearchDialog
          onClose={() => setSearchOpen(false)}
          onOpenThread={(thread) => {
            setSearchOpen(false)
            setSelected(thread)
          }}
        />
      )}

      <ComposeDialog
        open={composeOpen}
        devices={devices}
        onClose={() => setComposeOpen(false)}
        onSent={async (peer) => {
          setComposeOpen(false)
          toast.show('已发送', 'success')
          const rows = await loadThreads()
          const thread = rows?.find((t) => t.peer === peer)
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
          slotProps={{
            input: {
              startAdornment: (
                <InputAdornment position="start">
                  <SearchIcon fontSize="small" />
                </InputAdornment>
              ),
            }
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
            sx={{
              color: 'text.secondary',
              p: 4,
              textAlign: 'center'
            }}>
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
                    <Stack direction="row" spacing={1} sx={{
                      alignItems: 'baseline'
                    }}>
                      <Typography
                        variant="subtitle2"
                        noWrap
                        sx={{
                          flexGrow: 1,
                          fontWeight: thread.unread_count ? 700 : 600,
                        }}
                      >
                        {thread.peer || '(未知号码)'}
                      </Typography>
                      <Typography
                        variant="caption"
                        sx={{
                          color: 'text.secondary',
                          flexShrink: 0
                        }}>
                        {shortTime(thread.last_ts)}
                      </Typography>
                    </Stack>
                    <Typography
                      variant="body2"
                      noWrap
                      sx={{
                        mt: 0.25,
                        fontWeight: thread.unread_count ? 600 : 400,
                        color: thread.unread_count ? 'text.primary' : 'text.secondary',
                      }}
                    >
                      {thread.last_direction === 'out' && '你:'}
                      {threadPreview(thread)}
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
                      {Boolean(thread.unread_count) && (
                        <Chip
                          size="small"
                          label={thread.unread_count}
                          sx={{ height: 20, minWidth: 20, fontSize: '0.7rem', color: 'primary.contrastText', bgcolor: 'primary.main' }}
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
  onRead,
  onSent,
  onError,
}: {
  thread: Conversation | null
  devices: Device[]
  onBack?: () => void
  /** Fired after this thread's incoming messages are marked read. */
  onRead?: () => void
  onSent: () => void | Promise<void>
  onError: (message: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const bottom = useRef<HTMLDivElement | null>(null)

  // Keyed by the thread, so switching conversations swaps the cache entry
  // instead of clearing state in an effect — no null flash on a thread that
  // has already been read once.
  // How far back this thread has been read. Grows a whole window rather than
  // paging: a transcript has no page boundaries to land on, and an offset one
  // could gap or repeat if an SMS arrives mid-scroll. Remounting on a thread
  // switch resets it, so a new conversation starts at one window again.
  const [reach, setReach] = useState(THREAD_PAGE)

  const { data, mutate } = useSWR(
    thread ? ['/api/messages', thread.sim_id, thread.peer, reach] : null,
    async () => {
      const data = await api.messages.list({
        peer: thread!.peer,
        sim_id: thread!.sim_id ?? undefined,
        limit: reach,
      })
      // Opening a conversation is the read receipt.
      if (data.items.some((m) => m.direction === 'in' && !m.read_at)) {
        await api.messages.markRead(thread!.sim_id, thread!.peer)
        onRead?.()
      }
      // The API returns newest first; a conversation reads oldest first.
      return { messages: [...data.items].reverse(), total: data.total }
    },
    { keepPreviousData: true },
  )

  const messages = data?.messages
  const hasOlder = data ? hasOlderMessages(data.total, data.messages.length) : false

  const load = mutate

  // Per-thread draft. The parent gives this component a `key` derived from the
  // thread, so switching conversations remounts and the draft starts empty —
  // no effect needed to clear it.
  const [draft, setDraft] = useState('')

  // Keyed on the newest message, not the whole list: prepending older messages
  // must leave the scroll where it is. Depending on `messages` would jump the
  // operator back to the bottom on every "load older" — the opposite of the ask.
  const newestId = messages?.length ? messages[messages.length - 1].id : undefined
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [newestId])

  // Hooks stay unconditional while the empty-state render has no thread.
  const device = thread
    ? (devices.find(
        (d) => d.iccid && thread.sim_iccid && d.iccid === thread.sim_iccid,
      ) ?? devices.find((d) => d.name === thread.device))
    : undefined
  const online = Boolean(device?.online)

  const sendText = useCallback(
    async (body: string): Promise<boolean> => {
      if (!device || !thread) return false
      setBusy(true)
      try {
        await api.messages.send(device.name, thread.peer, body)
        await load()
        await onSent()
        return true
      } catch (err) {
        onError(err instanceof ApiError ? err.message : '发送失败')
        return false
      } finally {
        setBusy(false)
      }
    },
    [device, thread, load, onSent, onError],
  )

  if (!thread) {
    return (
      <Stack
        sx={{ flexGrow: 1, alignItems: 'center', justifyContent: 'center', p: 4 }}
        spacing={1}
      >
        <Typography variant="body1" sx={{
          color: 'text.secondary'
        }}>
          选择左边的一个会话
        </Typography>
        <Typography variant="caption" sx={{
          color: 'text.secondary'
        }}>
          或者点右上角「新会话」给一个新号码发短信
        </Typography>
      </Stack>
    )
  }

  const send = async () => {
    if (await sendText(draft)) setDraft('')
  }

  const retry = (message: Message) => {
    // Re-send the failed message verbatim; the toast from onSent confirms.
    void sendText(message.body)
  }

  return (
    <Stack sx={{ flexGrow: 1, minWidth: 0, minHeight: 0 }}>
      <Stack
        direction="row"
        spacing={1.5}
        sx={{
          alignItems: 'center',
          p: 1.5,
          flexShrink: 0
        }}>
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
          <Typography variant="caption" sx={{
            color: 'text.secondary'
          }}>
            {thread.sim_label || thread.sim_iccid || thread.device}
            {device && !online && ' · 模块离线'}
          </Typography>
        </Box>
      </Stack>
      <Divider />

      <Box sx={{ flexGrow: 1, overflowY: 'auto', px: 2, py: 2, minHeight: 0 }}>
        {!messages ? (
          <Loading />
        ) : (
          <Stack spacing={0.5}>
            {hasOlder && (
              <Box sx={{ alignSelf: 'center', pb: 1 }}>
                <Button
                  size="small"
                  onClick={() => setReach((current) => current + THREAD_PAGE)}
                >
                  加载更早的消息
                </Button>
              </Box>
            )}
            {messages.map((message, index) => (
              <Bubble
                key={message.id}
                message={message}
                showDay={index === 0 || !sameDay(messages[index - 1].ts, message.ts)}
                onRetry={retry}
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
          <Stack direction="row" spacing={1} sx={{
            alignItems: 'flex-start'
          }}>
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
            >
              <SendIcon />
            </IconButton>
          </Stack>
        )}
      </Box>
    </Stack>
  )
}

function Bubble({
  message,
  showDay,
  onRetry,
}: {
  message: Message
  showDay: boolean
  onRetry?: (message: Message) => void
}) {
  const outgoing = message.direction === 'out'
  const failed = message.status === 'failed'
  // Data, not text: an 8-bit TP-DCS or a port-addressing UDH (OTA
  // provisioning, WAP push, SIM toolkit). Decoding it as characters is what
  // produced the wall of mojibake this replaces, and an OTP search over it
  // would only ever find noise.
  const binary = Boolean(message.is_binary)
  const code = binary ? null : detectOtp(message.body)
  const [copied, setCopied] = useState(false)
  const [pduCopied, setPduCopied] = useState(false)

  /**
   * The raw PDU is the only thing that can settle why a message decoded the way
   * it did, and the modem's own copy is gone by the time anyone notices (the
   * agent deletes after reading). One click beats DevTools or a SQL prompt.
   */
  const copyPdu = async () => {
    if (!message.raw_pdu) return
    const detail = [
      `pdu=${message.raw_pdu}`,
      `dcs=${message.dcs ?? '(未记录)'}`,
      `binary=${binary ? 1 : 0}`,
      `segments=${message.segments}`,
    ].join('\n')
    try {
      await navigator.clipboard.writeText(detail)
    } catch {
      return
    }
    setPduCopied(true)
    setTimeout(() => setPduCopied(false), 1200)
  }

  const copy = async () => {
    if (!code) return
    try {
      await navigator.clipboard.writeText(code)
    } catch {
      // Clipboard needs a secure context; the copy button just won't work.
      return
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }

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
            {binary ? (
              <Stack direction="row" spacing={0.75} sx={{ alignItems: 'center' }}>
                <DataIcon fontSize="small" sx={{ opacity: 0.7 }} />
                <Typography variant="body2" sx={{ fontStyle: 'italic', opacity: 0.85 }}>
                  运营商数据短信,无文本内容
                </Typography>
              </Stack>
            ) : (
              <Typography variant="body2">{highlightOtp(message.body)}</Typography>
            )}
          </Box>
          <Stack
            direction="row"
            spacing={0.75}
            sx={{
              alignItems: 'center',
              justifyContent: outgoing ? 'flex-end' : 'flex-start',
              mt: 0.4,
              px: 0.5
            }}>
            {/* Spelled out, not just implied by which side it sits on. */}
            <Typography variant="caption" sx={{
              color: 'text.secondary'
            }}>
              {outgoing ? '发出' : '收到'} {clockTime(message.ts)}
            </Typography>
            {message.segments > 1 && (
              <Typography variant="caption" sx={{
                color: 'text.secondary'
              }}>
                · {message.segments} 段
              </Typography>
            )}
            {message.raw_pdu && (
              <Tooltip title={pduCopied ? '已复制' : '复制原始 PDU(排查用)'}>
                <IconButton
                  size="small"
                  onClick={() => void copyPdu()}
                  aria-label="复制原始 PDU"
                >
                  {pduCopied ? (
                    <CheckIcon sx={{ fontSize: 14, color: STATUS.good }} />
                  ) : (
                    <DataIcon sx={{ fontSize: 14, opacity: 0.5 }} />
                  )}
                </IconButton>
              </Tooltip>
            )}
            {failed && (
              <>
                <Tooltip title={message.error ?? '未知原因'}>
                  <Stack direction="row" spacing={0.25} sx={{
                    alignItems: 'center'
                  }}>
                    <ErrorIcon sx={{ fontSize: 14, color: STATUS.critical }} />
                    <Typography variant="caption" sx={{ color: STATUS.critical }}>
                      发送失败
                    </Typography>
                  </Stack>
                </Tooltip>
                {onRetry && (
                  <Tooltip title="重新发送">
                    <IconButton size="small" onClick={() => onRetry(message)} aria-label="重新发送">
                      <RefreshIcon sx={{ fontSize: 14 }} />
                    </IconButton>
                  </Tooltip>
                )}
              </>
            )}
            {code && (
              <Tooltip title={copied ? '已复制' : `复制验证码 ${code}`}>
                <IconButton size="small" onClick={() => void copy()} aria-label="复制验证码">
                  {copied ? (
                    <CheckIcon sx={{ fontSize: 14, color: STATUS.good }} />
                  ) : (
                    <ContentCopyIcon sx={{ fontSize: 14 }} />
                  )}
                </IconButton>
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
  // `null` = untouched, so the first module is a render-time fallback rather
  // than state an effect has to seed once the list arrives.
  const [picked, setPicked] = useState<string | null>(null)
  const [number, setNumber] = useState('')
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)

  const device =
    picked !== null && devices.some((d) => d.name === picked)
      ? picked
      : (devices[0]?.name ?? '')
  const setDevice = setPicked

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

// --------------------------------------------------------------------------
// full-text search — across every card and number, not just open threads
// --------------------------------------------------------------------------

function SearchDialog({
  onClose,
  onOpenThread,
}: {
  onClose: () => void
  onOpenThread: (thread: Conversation) => void
}) {
  const [query, setQuery] = useState('')

  // One request per typing pause, not per keystroke. The debounced term is
  // part of the key, so SWR also dedupes a term the user backspaces into
  // again and serves it from cache.
  const term = useDebounced(query.trim(), 250)
  const active = term.length >= 2

  const { data, error, isLoading } = useSWR(
    active ? ['/api/messages/search', term] : null,
    () => api.messages.list({ search: term, limit: 50 }),
    { keepPreviousData: false },
  )

  // A failed search reads as "no matches" rather than an empty frame; the
  // toast path does not reach into this dialog.
  const results = !active ? null : error ? [] : (data?.items ?? null)
  // A pending debounce counts as busy too, so a long-enough query never shows
  // "type a keyword" in the gap between the keystroke and the request.
  const busy = query.trim().length >= 2 && (term !== query.trim() || isLoading)

  const openThread = (message: Message) =>
    onOpenThread({
      sim_id: message.sim_id,
      peer: message.peer,
      device: message.device,
      last_id: message.id,
      last_body: message.body,
      last_direction: message.direction,
      last_status: message.status,
      last_ts: message.ts,
      message_count: 0,
      sim_label: message.sim_label,
      sim_iccid: message.sim_iccid,
    })

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>搜索全部短信</DialogTitle>
      <DialogContent>
        <TextField
          autoFocus
          fullWidth
          size="small"
          placeholder="号码或内容,至少 2 个字"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          slotProps={{
            input: {
              startAdornment: (
                <InputAdornment position="start">
                  <SearchIcon fontSize="small" />
                </InputAdornment>
              ),
            }
          }}
        />
        <Box sx={{ mt: 1.5 }}>
          {results === null ? (
            <Typography
              variant="body2"
              sx={{
                color: 'text.secondary',
                py: 3,
                textAlign: 'center'
              }}>
              {busy ? '搜索中…' : '输入关键词开始搜索'}
            </Typography>
          ) : results.length === 0 ? (
            <Typography
              variant="body2"
              sx={{
                color: 'text.secondary',
                py: 3,
                textAlign: 'center'
              }}>
              没有匹配的短信
            </Typography>
          ) : (
            <List disablePadding>
              {results.map((message) => (
                <ListItemButton
                  key={message.id}
                  onClick={() => openThread(message)}
                  sx={{ borderRadius: 2, alignItems: 'flex-start', py: 1, gap: 1.5 }}
                >
                  <Box sx={{ minWidth: 0, flexGrow: 1 }}>
                    <Stack direction="row" spacing={1} sx={{
                      alignItems: 'baseline'
                    }}>
                      <Typography variant="subtitle2" noWrap sx={{ flexGrow: 1 }}>
                        {message.peer}
                      </Typography>
                      <Typography
                        variant="caption"
                        sx={{
                          color: 'text.secondary',
                          flexShrink: 0
                        }}>
                        {message.sim_label || message.sim_iccid?.slice(-6)}
                      </Typography>
                    </Stack>
                    <Typography variant="body2" noWrap sx={{
                      color: 'text.secondary'
                    }}>
                      {message.direction === 'out' ? '你:' : '收到:'}
                      {message.body}
                    </Typography>
                  </Box>
                </ListItemButton>
              ))}
            </List>
          )}
        </Box>
      </DialogContent>
    </Dialog>
  )
}
