import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useCallback,
  type ReactNode,
} from 'react'
import useSWR from 'swr'
import useSWRInfinite from 'swr/infinite'
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
  ToggleButton,
  ToggleButtonGroup,
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
import WarningIcon from '@mui/icons-material/WarningAmberOutlined'
import CheckIcon from '@mui/icons-material/CheckOutlined'
import {
  deliveryStatusLabel,
  detectOtp,
  mergeThreadPages,
  messagePreview,
  OTP_RE,
  resolveThread,
  resolveThreadDevice,
  threadPreview,
  threadScope,
} from '../messages'
import {
  api,
  ApiError,
  errorText,
  type Conversation,
  type Device,
  type Message,
  type MessagePage,
} from '../api'
import { useToast } from '../toast'
import { Loading, RefreshNotice } from '../components/common'
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
type MessageContent = 'all' | 'text' | 'data'
// Roughly where a GSM-7 message splits.  Only a hint: the agent does the real
// segmentation, and Unicode content splits far earlier.
const SINGLE_SEGMENT = 70

/** Identity of a thread as a string, for the draft store. */
const threadKey = (thread: Pick<Conversation, 'peer' | 'sim_id'>) =>
  `${thread.sim_id ?? 'unassigned'}:${thread.peer}`

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
  const [content, setContent] = useState<MessageContent>('all')
  // Drafts live above the thread view, which remounts on every thread switch:
  // an unsent reply has to survive a glance at another conversation. Memory
  // only — an SMS body is not something to leave in localStorage, and this
  // whole subtree unmounts on logout.
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const setDraft = useCallback(
    (thread: Pick<Conversation, 'peer' | 'sim_id'>, text: string) =>
      setDrafts((current) => ({ ...current, [threadKey(thread)]: text })),
    [],
  )

  const {
    data: threads,
    error: threadsError,
    mutate: loadThreads,
  } = useSWR(
    ['/api/messages/conversations', content],
    () => api.messages.conversations(content === 'all' ? undefined : content),
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
        // A damaged thread's `last_body` is mojibake, so the words the user can
        // actually see in the preview live only here. Matching the same text the
        // list renders is the point — typing what is on screen has to work.
        (t.last_recovered_body ?? '').toLowerCase().includes(needle) ||
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
              <IconButton
                onClick={() =>
                  // The same filter the list is showing — an export that
                  // silently covered a different set than the screen it was
                  // started from is the bug this scope object exists to prevent.
                  void api.messages.exportCsv({
                    content: content === 'all' ? undefined : content,
                  })
                }
                size="small"
              >
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
            error={threadsError}
            onRetry={loadThreads}
            selected={selected}
            onSelect={setSelected}
            search={search}
            onSearch={setSearch}
            content={content}
            onContent={(next) => {
              setContent(next)
              setSelected(null)
            }}
            fullWidth={narrow}
          />
        )}
        {showList && showThread && <Divider orientation="vertical" flexItem />}
        {showThread && (
          <ThreadView
            key={selected ? `${selected.sim_id}:${selected.peer}:${content}` : 'empty'}
            thread={selected}
            content={content}
            devices={devices}
            draft={selected ? drafts[threadKey(selected)] ?? '' : ''}
            onDraft={(text) => selected && setDraft(selected, text)}
            onBack={narrow ? () => setSelected(null) : undefined}
            onRead={() => void loadThreads()}
            onSent={async () => {
              toast.show('已发送', 'success')
              if (content === 'all') await loadThreads()
              else setContent('all')
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
            // Search spans both text and data regardless of the list filter.
            // Show the result the operator actually chose.
            setContent('all')
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
          const rows = content === 'all'
            ? await loadThreads()
            : await api.messages.conversations()
          setContent('all')
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
  error,
  onRetry,
  selected,
  onSelect,
  search,
  onSearch,
  content,
  onContent,
  fullWidth,
}: {
  threads: Conversation[] | null
  /** A failed conversations read; the list must not look merely empty. */
  error?: unknown
  onRetry?: () => unknown
  selected: Conversation | null
  onSelect: (thread: Conversation) => void
  search: string
  onSearch: (value: string) => void
  content: MessageContent
  onContent: (value: MessageContent) => void
  fullWidth: boolean
}) {
  return (
    <Stack sx={{ width: fullWidth ? '100%' : 340, flexShrink: 0, minHeight: 0 }}>
      <Stack spacing={1} sx={{ p: 1.5 }}>
        <ToggleButtonGroup
          exclusive
          size="small"
          value={content}
          onChange={(_, value: MessageContent | null) => value && onContent(value)}
          aria-label="短信内容类型"
          sx={{ '& .MuiToggleButton-root': { flex: 1 } }}
        >
          <ToggleButton value="all">全部</ToggleButton>
          <ToggleButton value="text">文本</ToggleButton>
          <ToggleButton value="data">数据</ToggleButton>
        </ToggleButtonGroup>
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
      </Stack>
      <Divider />
      <Box sx={{ overflowY: 'auto', flexGrow: 1 }}>
        {/* A stale list is the difference between "no new messages" and "we have
            not been able to ask" — on this page the operator is usually waiting
            for a code that is supposed to arrive within seconds. */}
        <RefreshNotice
          data={threads}
          error={error}
          onRetry={onRetry}
          sx={{ mx: 2, mt: 2, mb: 0 }}
        />
        {threads === null ? (
          error ? null : <Loading />
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
  content,
  devices,
  draft,
  onDraft,
  onBack,
  onRead,
  onSent,
  onError,
}: {
  thread: Conversation | null
  content: MessageContent
  devices: Device[]
  /** Held by the page, so an unsent reply survives a look at another thread. */
  draft: string
  onDraft: (text: string) => void
  onBack?: () => void
  /** Fired after this thread's incoming messages are marked read. */
  onRead?: () => void
  onSent: () => void | Promise<void>
  onError: (message: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const bottom = useRef<HTMLDivElement | null>(null)
  const scroller = useRef<HTMLDivElement | null>(null)

  const scope = thread ? threadScope(thread.sim_id) : null
  const kind = content === 'all' ? undefined : content

  // Fixed pages walked backwards by cursor, not one window that grows.
  //
  // The window it replaces re-read the whole conversation every five seconds
  // and, past the server's 2,000-row cap, answered 422 — a dead end in the
  // middle of a history the operator was entitled to read. Each page here is
  // cached under its own cursor, so paging back is additive: only page 0, the
  // live tail, is revalidated, and `(ts,id)` boundaries cannot gap or repeat
  // when an SMS lands mid-scroll the way an offset can.
  const { data: pages, error: pagesError, size, setSize, mutate, isValidating } =
    useSWRInfinite<MessagePage>(
    (index, previous: MessagePage | null) => {
      if (!thread || scope === null) return null
      if (index === 0) return ['/api/messages', scope, thread.peer, content, '']
      // A page with nothing older behind it ends the walk.
      if (!previous?.next_cursor) return null
      return ['/api/messages', scope, thread.peer, content, previous.next_cursor]
    },
    ([, , peer, , before]: [string, typeof scope, string, MessageContent, string]) =>
      api.messages.list({
        scope: scope ?? 'all',
        peer,
        content: kind,
        limit: THREAD_PAGE,
        before: before || undefined,
        // The transcript re-reads itself every few seconds; counting a
        // 10,000-message history each time answers nothing `has_more` doesn't.
        count: false,
      }),
    {
      keepPreviousData: true,
      // Only the tail. Older pages are immutable history — re-reading them on
      // every tick is what made a long conversation expensive.
      revalidateFirstPage: true,
      revalidateAll: false,
      // Delivery reports arrive independently from the send request. Keep the
      // open transcript live so pending bubbles settle without a manual refresh.
      refreshInterval: 5_000,
    },
  )

  // The API returns newest first, a conversation reads oldest first, and the
  // pages overlap once the tail grows — see `mergeThreadPages`.
  const messages = useMemo(() => mergeThreadPages(pages), [pages])
  const loaded = pages !== undefined
  const hasOlder = pages?.[pages.length - 1]?.has_more ?? false
  const loadingOlder = size > (pages?.length ?? 0) && isValidating

  const load = mutate

  // Follow the tail only when the operator is already at it. `scrollIntoView`
  // on every new message used to yank the view out from under someone reading
  // history — the new arrival is announced instead, and they choose when to go.
  const [atBottom, setAtBottom] = useState(true)
  // Where the operator's eyes last were, frozen at the moment they scrolled away
  // from the tail. While they are *at* the tail there is nothing to remember —
  // the newest message is on screen by definition, so `seen` derives from it.
  const [frozen, setFrozen] = useState(0)

  const newest = messages.length ? messages[messages.length - 1] : undefined
  const newestId = newest?.id
  useEffect(() => {
    if (!newestId || !atBottom) return
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [newestId, atBottom])

  // The newest message the operator has actually had in front of them. Both the
  // "N 条新消息" count and the read watermark are measured from here.
  const seen = atBottom ? newestId ?? 0 : frozen

  /** Back to the tail, releasing the frozen mark so `seen` follows it again. */
  const followTail = useCallback(() => {
    setAtBottom(true)
    setFrozen(0)
  }, [])

  const unseen = useMemo(() => {
    if (atBottom || !seen) return 0
    const index = messages.findIndex((message) => message.id === seen)
    return index < 0 ? 0 : messages.length - 1 - index
  }, [messages, atBottom, seen])

  // Reading is a deliberate act, not a side effect of a fetch.
  //
  // Marking read inside the fetcher meant a background revalidation — or a
  // prefetch of older pages — could read a thread nobody was looking at, and
  // the watermark it sent was "whatever the query returned", including a code
  // that landed between the render and the request. This advances only what is
  // on screen, only while this tab is in the foreground, and only as far as the
  // newest message the operator has seen.
  const [foreground, setForeground] = useState(
    () => typeof document === 'undefined' || document.visibilityState === 'visible',
  )
  useEffect(() => {
    const onVisibility = () => setForeground(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [])

  const reported = useRef(0)
  useEffect(() => {
    if (!thread || scope === null || !foreground) return
    // The seen mark, or — for a thread scrolled away from before it ever
    // settled — the newest row already in hand. Either way a known watermark,
    // never "everything this filter can reach".
    const watermark = seen || newestId
    if (!watermark || watermark <= reported.current) return
    const unread = messages.some(
      (message) =>
        message.direction === 'in' && !message.read_at && message.id <= watermark,
    )
    if (!unread) return
    reported.current = watermark
    void api.messages
      .markRead(scope, thread.peer, watermark, kind)
      .then(() => onRead?.())
      // A failed read receipt must be retryable, not swallowed for the life of
      // the thread — the badge would then disagree with the transcript.
      .catch(() => {
        reported.current = 0
      })
  }, [thread, scope, kind, foreground, messages, newestId, seen, onRead])

  // Keep the first visible message where it is when older ones are prepended.
  // Measured from the bottom, which is the edge that does not move.
  const anchor = useRef<number | null>(null)
  const loadOlder = useCallback(() => {
    const element = scroller.current
    anchor.current = element ? element.scrollHeight - element.scrollTop : null
    void setSize((current) => current + 1)
  }, [setSize])

  useLayoutEffect(() => {
    const element = scroller.current
    if (!element || anchor.current === null) return
    element.scrollTop = element.scrollHeight - anchor.current
    anchor.current = null
  }, [messages.length])

  const onScroll = useCallback(() => {
    const element = scroller.current
    if (!element) return
    const bottomGap = element.scrollHeight - element.scrollTop - element.clientHeight
    const pinned = bottomGap < 80
    setAtBottom(pinned)
    // Leaving the tail freezes the seen mark at what was on screen; returning
    // releases it. Anything that arrives in between is counted, not scrolled to.
    setFrozen((current) => (pinned ? 0 : current || newestId || 0))
    // Reaching the top continues the walk on its own; the button below stays
    // for keyboard and screen-reader users, who never generate this event.
    if (element.scrollTop < 120 && hasOlder && !loadingOlder) loadOlder()
  }, [newestId, hasOlder, loadingOlder, loadOlder])

  // Hooks stay unconditional while the empty-state render has no thread.
  // The card is the reliable link; the module name is only a fallback for a
  // thread with no SIM, and only when exactly one module answers to it — two
  // agents can each have a `modem-1`, and replying through the wrong one would
  // send from the wrong card.
  const device = thread ? resolveThreadDevice(devices, thread) : undefined
  const online = Boolean(device?.online)

  const sendText = useCallback(
    async (body: string): Promise<boolean> => {
      if (!device || !thread) return false
      setBusy(true)
      try {
        await api.messages.send(device.id, thread.peer, body)
        // Your own message is the one exception to not stealing the scroll:
        // sending is a request to be at the bottom.
        followTail()
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
    [device, thread, load, onSent, onError, followTail],
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
    if (await sendText(draft)) onDraft('')
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

      <Box
        ref={scroller}
        onScroll={onScroll}
        sx={{ flexGrow: 1, overflowY: 'auto', px: 2, py: 2, minHeight: 0, position: 'relative' }}
      >
        {!loaded ? (
          pagesError ? (
            <Alert
              severity="warning"
              variant="outlined"
              action={
                <Button color="inherit" size="small" onClick={() => void load()}>
                  重试
                </Button>
              }
            >
              这段对话没能读出来({errorText(pagesError)})。
            </Alert>
          ) : (
            <Loading />
          )
        ) : (
          <Stack spacing={0.5}>
            {/* The transcript re-reads itself every few seconds; when that stops
                working, an operator waiting on a verification code has to know
                that "no new message" is not an answer. */}
            <RefreshNotice data={pages} error={pagesError} onRetry={load} sx={{ mb: 1 }} />
            {hasOlder && (
              <Box sx={{ alignSelf: 'center', pb: 1 }}>
                <Button size="small" onClick={loadOlder} disabled={loadingOlder}>
                  {loadingOlder ? '正在加载…' : '加载更早的消息'}
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
      {/* Arrivals while the operator is reading history: announced, never
          scrolled to. Clicking is what moves the view. */}
      {unseen > 0 && (
        <Box sx={{ position: 'relative', height: 0, textAlign: 'center' }}>
          <Chip
            size="small"
            color="primary"
            clickable
            label={`有 ${unseen} 条新消息`}
            onClick={() => {
              followTail()
              bottom.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
            }}
            sx={{ position: 'absolute', bottom: 8, left: '50%', transform: 'translateX(-50%)' }}
          />
        </Box>
      )}

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
              onChange={(e) => onDraft(e.target.value)}
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
  const deliveryLabel = outgoing ? deliveryStatusLabel(message.status) : null
  const deliveryColor = message.status === 'delivered'
    ? STATUS.good
    : message.status === 'partial'
      ? STATUS.warning
      : message.status === 'failed'
        ? STATUS.critical
        : 'text.secondary'
  // Data, not text: an 8-bit TP-DCS, a port-addressing UDH (OTA provisioning,
  // WAP push, SIM toolkit), a malformed UDH with no trustworthy payload
  // boundary, or an empty operator control message. Decoding data as characters
  // is what produced the wall of mojibake this replaces.
  const binary = Boolean(message.is_binary)
  // Damaged is a separate state from data, and has to be checked first: the
  // modem dropped octets out of the frame, so `body` was decoded under header
  // fields that are really message body. It is mojibake — hence `is_binary` —
  // but a person wrote it, and the agent re-phased part of it back out.
  const damaged = Boolean(message.truncated)
  const salvaged = (message.recovered_body || '').trim()
  // A code the agent recovered still gets a copy button. Running `detectOtp`
  // over the mojibake instead would offer digits that are decoder noise, so
  // for a damaged message only the salvage is trusted.
  const code = damaged
    ? (message.recovered_code || '').trim() || null
    : binary
      ? null
      : detectOtp(message.body)
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
            {damaged ? (
              <Stack spacing={0.5}>
                {/* Says it is a fragment every time, including when nothing was
                    recovered. A fragment rendered as if it were the whole
                    message is worse than no message: the reader takes "no code
                    in here" at face value, when the code was most likely in the
                    head the decoder cannot reach. */}
                <Stack direction="row" spacing={0.75} sx={{ alignItems: 'center' }}>
                  <WarningIcon sx={{ fontSize: 16, color: STATUS.warning }} />
                  <Typography
                    variant="caption"
                    sx={{ color: STATUS.warning, fontWeight: 600 }}
                  >
                    短信在模组内损坏,以下不是全文
                  </Typography>
                </Stack>
                {salvaged ? (
                  <Typography variant="body2">{highlightOtp(salvaged)}</Typography>
                ) : (
                  <Typography variant="body2" sx={{ fontStyle: 'italic', opacity: 0.85 }}>
                    正文未能恢复,可复制原始 PDU 排查。
                  </Typography>
                )}
              </Stack>
            ) : binary ? (
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
            {deliveryLabel && !failed && (
              <Typography variant="caption" sx={{ color: deliveryColor, fontWeight: 600 }}>
                · {deliveryLabel}
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
                      {deliveryLabel ?? '发送失败'}
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
              <Tooltip
                title={
                  copied
                    ? '已复制'
                    // Hedged for a damaged message: this came out of a
                    // re-phasing pass, and the digits either side of the hole
                    // are gone.
                    : `${damaged ? '复制救回的可能验证码' : '复制验证码'} ${code}`
                }
              >
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
  // than state an effect has to seed once the list arrives.  The id, not the
  // name: the name is ambiguous once two agents each have a `modem-1`, and the
  // select would then bind to whichever of them rendered first.
  const [picked, setPicked] = useState<number | null>(null)
  const [number, setNumber] = useState('')
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)

  const device =
    devices.find((d) => d.id === picked) ?? devices[0]
  const deviceId = device?.id ?? 0

  const send = async () => {
    setBusy(true)
    try {
      if (!deviceId) return
      await api.messages.send(deviceId, number, body)
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

  const online = device?.online

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>新会话</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            select
            label="使用哪张卡"
            value={deviceId ? String(deviceId) : ''}
            onChange={(e) => setPicked(Number(e.target.value))}
            fullWidth
          >
            {devices.map((d) => (
              <MenuItem key={d.id} value={String(d.id)} disabled={!d.online}>
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
          disabled={busy || !deviceId || !number || !body || !online}
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
    // No total: the dialog shows the best 50 hits, and counting every match of
    // a LIKE over the whole history on each pause is work nothing displays.
    () => api.messages.list({ search: term, limit: 50, count: false }),
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
      last_is_binary: message.is_binary,
      // Without these the preview of a damaged hit reads "运营商数据短信" until
      // the conversations list refetches and replaces this optimistic row.
      last_truncated: message.truncated,
      last_recovered_body: message.recovered_body,
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
                      {messagePreview(message)}
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
