import { useCallback, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  IconButton,
  MenuItem,
  Stack,
  Switch,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import DeleteIcon from '@mui/icons-material/DeleteOutlined'
import EditIcon from '@mui/icons-material/EditOutlined'
import SendIcon from '@mui/icons-material/SendOutlined'
import useSWR from 'swr'
import {
  api,
  ApiError,
  type Channel,
  type ChannelInput,
  type NotifyLog,
  type Rule,
  type RuleInput,
  type RulePreview,
  type Sim,
} from '../api'
import { formatTs } from '../format'
import { useToast } from '../toast'
import { Loading } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { LIVE_MS } from '../swr'
import { STATUS } from '../tokens'

interface FieldSpec {
  key: string
  label: string
  hint?: string
  optional?: boolean
  password?: boolean
}

/** Channel type -> the settings the server actually reads out of `config`. */
const CHANNEL_TYPES: { value: string; label: string; fields: FieldSpec[] }[] = [
  {
    value: 'bark',
    label: 'Bark (iOS)',
    fields: [{ key: 'url', label: '推送地址', hint: 'https://api.day.app/你的key' }],
  },
  {
    value: 'telegram',
    label: 'Telegram',
    fields: [
      { key: 'token', label: 'Bot Token', hint: '从 @BotFather 拿到' },
      { key: 'chat_id', label: 'Chat ID' },
    ],
  },
  {
    value: 'feishu',
    label: '飞书机器人',
    fields: [
      { key: 'webhook', label: 'Webhook 地址', hint: 'https://open.feishu.cn/open-apis/bot/v2/hook/...' },
      { key: 'secret', label: '签名密钥', optional: true, hint: '机器人开了「签名校验」才需要填' },
    ],
  },
  {
    value: 'wecom',
    label: '企业微信',
    fields: [
      { key: 'webhook', label: 'Webhook 地址', hint: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...' },
    ],
  },
  {
    value: 'dingtalk',
    label: '钉钉',
    fields: [
      { key: 'webhook', label: 'Webhook 地址', hint: 'https://oapi.dingtalk.com/robot/send?access_token=...' },
      { key: 'secret', label: '加签密钥', optional: true, hint: '安全设置选「加签」才需要填' },
    ],
  },
  {
    value: 'post',
    label: '自定义 POST',
    fields: [
      { key: 'url', label: 'URL', hint: '收到 JSON:sender / message / timestamp / card / device / text' },
    ],
  },
  {
    value: 'get',
    label: '自定义 GET',
    fields: [{ key: 'url', label: 'URL', hint: '同名参数会并到地址已有的 query 上' }],
  },
  {
    value: 'smtp',
    label: '邮件 (SMTP)',
    fields: [
      { key: 'host', label: 'SMTP 服务器', hint: 'smtp.qq.com' },
      { key: 'to', label: '收件人', hint: '多个用逗号分隔' },
      { key: 'security', label: '加密方式', optional: true, hint: 'ssl(默认) / starttls / none' },
      { key: 'port', label: '端口', optional: true, hint: '留空:ssl 用 465,starttls 用 587' },
      { key: 'username', label: '账号', optional: true },
      { key: 'password', label: '密码 / 授权码', optional: true, password: true },
      { key: 'from', label: '发件人', optional: true, hint: '留空则用账号' },
    ],
  },
]

const TEMPLATE_HINT = '可用变量:{card} {sender} {message} {timestamp} {device} {iccid}'

function typeLabel(type: string): string {
  return CHANNEL_TYPES.find((t) => t.value === type)?.label ?? type
}

function parseConfig(channel: Channel): Record<string, string> {
  try {
    const parsed = JSON.parse(channel.config || '{}')
    return typeof parsed === 'object' && parsed ? parsed : {}
  } catch {
    return {}
  }
}

export function NotifyPage() {
  const toast = useToast()
  const [channelEdit, setChannelEdit] = useState<Channel | null | undefined>(undefined)
  const [ruleEdit, setRuleEdit] = useState<Rule | null | undefined>(undefined)
  const [testing, setTesting] = useState<number | null>(null)
  const [tab, setTab] = useState(0)

  const { data: channels, mutate: mutateChannels } = useSWR('/api/channels', () =>
    api.channels.list(),
  )
  const { data: rules = [], mutate: mutateRules } = useSWR('/api/rules', () => api.rules.list())
  const { data: sims = [] } = useSWR('/api/sims', () => api.sims.list())
  // "最近投递" is a summary card, not a log browser — the Logs page carries the
  // full paged view. Ask for exactly the 50 rows it renders.
  const { data: notifyPage } = useSWR(
    '/api/notify-logs?recent',
    () => api.notifyLogs('limit=50&offset=0'),
    { refreshInterval: LIVE_MS },
  )

  // Rules carry a denormalised `channel_name`, so renaming or deleting a
  // channel has to refetch both lists, not just the one that was written.
  const load = useCallback(
    () => Promise.all([mutateChannels(), mutateRules()]),
    [mutateChannels, mutateRules],
  )

  const runTest = async (channel: Channel) => {
    setTesting(channel.id)
    try {
      await api.channels.test(channel.id)
      toast.show(`${channel.name}:测试消息已发出`, 'success')
    } catch (err) {
      // The provider's own complaint is the useful part — show it verbatim.
      toast.show(err instanceof ApiError ? `推送失败:${err.message}` : '推送失败', 'error')
    } finally {
      setTesting(null)
      await load()
    }
  }

  const toggleChannel = async (channel: Channel) => {
    const config = parseConfig(channel)
    await api.channels.update(channel.id, {
      name: channel.name,
      type: channel.type,
      config,
      enabled: !channel.enabled,
    })
    await load()
  }

  const toggleRule = async (rule: Rule) => {
    await api.rules.update(rule.id, { ...ruleToInput(rule), enabled: !rule.enabled })
    await load()
  }

  if (!channels) return <Loading />

  return (
    <Stack spacing={3}>
      <PageHeader title="通知" subtitle="把短信路由到 Bark、Telegram、飞书等渠道" />

      <Alert severity="info">
        推送由 <strong>服务器</strong> 发出,走机房网络 —— 不消耗 SIM 卡流量,所以纯保号卡也能用。
      </Alert>

      <Box sx={{ borderBottom: 1, borderColor: 'divider' }}>
        <Tabs value={tab} onChange={(_, next) => setTab(next)} variant="scrollable" scrollButtons="auto">
          <Tab label={`渠道 (${channels.length})`} />
          <Tab label={`转发规则 (${rules.length})`} />
          <Tab label="模板测试" />
          <Tab label="投递记录" />
        </Tabs>
      </Box>

      {tab === 0 && <Card>
        <CardHeader
          title={<Typography variant="h3">推送渠道</Typography>}
          action={
            <Button size="small" startIcon={<AddIcon />} onClick={() => setChannelEdit(null)}>
              新建渠道
            </Button>
          }
        />
        <CardContent sx={{ pt: 0 }}>
          {channels.length === 0 ? (
            <Typography
              variant="body2"
              sx={{
                color: 'text.secondary',
                py: 2
              }}>
              还没有渠道。先建一个,再用规则把短信路由过去。
            </Typography>
          ) : (
            <TableContainer>
              <Table size="small" sx={{ minWidth: 420 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>名称</TableCell>
                    <TableCell>类型</TableCell>
                    <TableCell>启用</TableCell>
                    <TableCell align="right" />
                  </TableRow>
                </TableHead>
              <TableBody>
                {channels.map((channel) => (
                  <TableRow key={channel.id}>
                    <TableCell>{channel.name}</TableCell>
                    <TableCell>{typeLabel(channel.type)}</TableCell>
                    <TableCell>
                      <Switch
                        size="small"
                        checked={Boolean(channel.enabled)}
                        onChange={() => void toggleChannel(channel)}
                        slotProps={{
                          input: { 'aria-label': `启用 ${channel.name}` }
                        }}
                      />
                    </TableCell>
                    <TableCell align="right">
                      <Tooltip title="发一条测试消息">
                        <span>
                          <IconButton
                            size="small"
                            aria-label="测试"
                            disabled={testing !== null}
                            onClick={() => void runTest(channel)}
                          >
                            {testing === channel.id ? (
                              <CircularProgress size={16} />
                            ) : (
                              <SendIcon fontSize="small" />
                            )}
                          </IconButton>
                        </span>
                      </Tooltip>
                      <IconButton
                        size="small"
                        aria-label="编辑"
                        onClick={() => setChannelEdit(channel)}
                      >
                        <EditIcon fontSize="small" />
                      </IconButton>
                      <IconButton
                        size="small"
                        aria-label="删除"
                        onClick={async () => {
                          await api.channels.remove(channel.id)
                          await load()
                        }}
                      >
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            </TableContainer>
          )}
        </CardContent>
      </Card>}

      {tab === 1 && <Card>
        <CardHeader
          title={<Typography variant="h3">转发规则</Typography>}
          action={
            <Button
              size="small"
              startIcon={<AddIcon />}
              onClick={() => setRuleEdit(null)}
              disabled={channels.length === 0}
            >
              新建规则
            </Button>
          }
        />
        <CardContent sx={{ pt: 0 }}>
          {rules.length === 0 ? (
            <Typography
              variant="body2"
              sx={{
                color: 'text.secondary',
                py: 2
              }}>
              还没有规则。没有规则时不会推送任何短信。
            </Typography>
          ) : (
            <TableContainer>
              <Table size="small" sx={{ minWidth: 560 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>名称</TableCell>
                    <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                      范围
                    </TableCell>
                    <TableCell>匹配</TableCell>
                    <TableCell>渠道</TableCell>
                    <TableCell>启用</TableCell>
                    <TableCell align="right" />
                  </TableRow>
                </TableHead>
              <TableBody>
                {rules.map((rule) => (
                  <TableRow key={rule.id}>
                    <TableCell>{rule.name || `规则 ${rule.id}`}</TableCell>
                    <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                      {rule.sim_id
                        ? sims.find((s) => s.id === rule.sim_id)?.label || `卡 ${rule.sim_id}`
                        : '全部卡'}
                    </TableCell>
                    <TableCell>
                      {rule.match === 'all'
                        ? '全部短信'
                        : `${rule.match === 'keyword' ? '关键词' : '正则'}:${rule.pattern}`}
                    </TableCell>
                    <TableCell>
                      {rule.channel_name}
                      {channels.find((c) => c.id === rule.channel_id)?.enabled === 0 && (
                        <Chip
                          size="small"
                          variant="outlined"
                          label="渠道已停用"
                          sx={{ ml: 1, color: STATUS.warning }}
                        />
                      )}
                    </TableCell>
                    <TableCell>
                      <Switch
                        size="small"
                        checked={Boolean(rule.enabled)}
                        onChange={() => void toggleRule(rule)}
                        slotProps={{
                          input: { 'aria-label': `启用规则 ${rule.id}` }
                        }}
                      />
                    </TableCell>
                    <TableCell align="right">
                      <IconButton
                        size="small"
                        aria-label="编辑"
                        onClick={() => setRuleEdit(rule)}
                      >
                        <EditIcon fontSize="small" />
                      </IconButton>
                      <IconButton
                        size="small"
                        aria-label="删除"
                        onClick={async () => {
                          await api.rules.remove(rule.id)
                          await load()
                        }}
                      >
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            </TableContainer>
          )}
        </CardContent>
      </Card>}

      {tab === 1 && <Typography variant="caption" sx={{
        color: 'text.secondary'
      }}>
        一条短信命中多条规则时,同一个渠道只推一次(取优先级最高的模板)。推送结果见「日志 →
        推送日志」。
      </Typography>}

      {tab === 2 && <RuleTesterCard sims={sims} onError={(m) => toast.show(m, 'error')} />}

      {tab === 3 && (
        <Stack spacing={2}>
          <DeliveryLogCard logs={notifyPage?.items ?? []} />
          <SettingsCard
            onError={(m) => toast.show(m, 'error')}
            onSaved={() => toast.show('设置已保存', 'success')}
          />
        </Stack>
      )}

      {channelEdit !== undefined && (
        <ChannelDialog
          channel={channelEdit}
          onClose={() => setChannelEdit(undefined)}
          onSaved={async (created) => {
            setChannelEdit(undefined)
            await load()
            toast.show(created ? '渠道已创建' : '渠道已保存', 'success')
          }}
          onError={(m) => toast.show(m, 'error')}
        />
      )}
      {ruleEdit !== undefined && (
        <RuleDialog
          rule={ruleEdit}
          channels={channels}
          sims={sims}
          onClose={() => setRuleEdit(undefined)}
          onSaved={async (created) => {
            setRuleEdit(undefined)
            await load()
            toast.show(created ? '规则已创建' : '规则已保存', 'success')
          }}
          onError={(m) => toast.show(m, 'error')}
        />
      )}
      {toast.element}
    </Stack>
  )
}

function RuleTesterCard({
  sims,
  onError,
}: {
  sims: Sim[]
  onError: (message: string) => void
}) {
  const [simId, setSimId] = useState<string>('')
  const [peer, setPeer] = useState('')
  const [body, setBody] = useState('')
  const [result, setResult] = useState<RulePreview[] | null>(null)
  const [busy, setBusy] = useState(false)
  const selectedSim = sims.find((sim) => sim.id === Number(simId))
  const cardLabel = selectedSim?.label || (selectedSim?.iccid ? `…${selectedSim.iccid.slice(-6)}` : '卡片')
  const sender = peer.trim() || '发件人'
  const rendered = result?.[0]
  const defaultText = `【${cardLabel}】${sender}\n${body}`
  const previewText = rendered?.text === defaultText ? body : rendered?.text || body || '短信正文'
  const previewTitle = rendered?.title || `${cardLabel} · ${sender}`

  const preview = async () => {
    setBusy(true)
    try {
      const parsed = simId ? Number(simId) : null
      setResult(await api.rules.preview(parsed, peer.trim(), body))
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '预览失败')
      setResult([])
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader
        title={<Typography variant="h3">规则调试器</Typography>}
        subheader={
          <Typography variant="caption" sx={{
            color: 'text.secondary'
          }}>
            粘贴一条真实短信,看它会命中哪些规则、实际推出去长什么样 —— 不真的发送。
          </Typography>
        }
      />
      <CardContent>
        <Stack spacing={3}>
          {/* Input: one compact panel across the full width, so the two output
              regions below get the whole card to themselves. */}
          <Box
            sx={{
              p: 2,
              borderRadius: 1.5,
              border: 1,
              borderColor: 'divider',
              bgcolor: 'background.default',
            }}
          >
            <Stack spacing={2}>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                <TextField
                  select
                  size="small"
                  label="卡(留空 = 全部卡)"
                  value={simId}
                  onChange={(e) => setSimId(e.target.value)}
                  sx={{ minWidth: 160, flex: 1 }}
                >
                  <MenuItem value="">全部卡</MenuItem>
                  {sims.map((sim) => (
                    <MenuItem key={sim.id} value={sim.id}>
                      {sim.label || sim.iccid.slice(-6)}
                    </MenuItem>
                  ))}
                </TextField>
                <TextField
                  size="small"
                  label="发件号码(可选)"
                  value={peer}
                  onChange={(e) => setPeer(e.target.value)}
                  placeholder="10086"
                  sx={{ minWidth: 160, flex: 1 }}
                />
              </Stack>
              <TextField
                size="small"
                label="短信内容"
                value={body}
                onChange={(e) => setBody(e.target.value)}
                placeholder="【移动】验证码 123456,请勿泄露"
                multiline
                minRows={2}
                maxRows={6}
                fullWidth
              />
              <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
                <Button
                  variant="contained"
                  onClick={() => void preview()}
                  disabled={busy || body.trim().length === 0}
                  sx={{ whiteSpace: 'nowrap' }}
                >
                  预览命中
                </Button>
              </Box>
            </Stack>
          </Box>

          {/* Results and the card preview sit side by side on wide screens and
              stack on narrow ones — the preview is no longer a fixed 340px rail. */}
          <Box
            sx={{
              display: 'grid',
              gridTemplateColumns: { xs: '1fr', lg: 'minmax(0, 1fr) minmax(0, 0.8fr)' },
              gap: 3,
              alignItems: 'start',
            }}
          >
            <Stack spacing={1.5}>
              <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                命中规则{result && result.length > 0 ? `(${result.length})` : ''}
              </Typography>
              {busy ? (
                <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
                  <CircularProgress size={24} />
                </Box>
              ) : result === null ? (
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  填好上面的内容,点「预览命中」看结果。
                </Typography>
              ) : result.length === 0 ? (
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  没有规则命中。想收到这条短信的话,先建一条匹配它的规则。
                </Typography>
              ) : (
                result.map((hit) => (
                  <Box
                    key={hit.rule_id}
                    sx={{
                      border: 1,
                      borderColor: 'divider',
                      borderRadius: 1.5,
                      overflow: 'hidden',
                    }}
                  >
                    <Stack
                      direction="row"
                      spacing={1}
                      sx={{
                        alignItems: 'center',
                        flexWrap: 'wrap',
                        rowGap: 0.5,
                        px: 1.5,
                        py: 1,
                        bgcolor: 'background.default',
                      }}
                    >
                      <Chip
                        size="small"
                        label={hit.rule_name || `规则 ${hit.rule_id}`}
                        color="primary"
                        variant="outlined"
                      />
                      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                        → {hit.channel_name}
                      </Typography>
                      <Box sx={{ flexGrow: 1 }} />
                      <Typography
                        variant="caption"
                        sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}
                      >
                        优先级 {hit.priority}
                      </Typography>
                    </Stack>
                    <Divider />
                    <Stack spacing={1.5} sx={{ p: 1.5 }}>
                      <Box>
                        <Typography
                          variant="caption"
                          sx={{ color: 'text.secondary', display: 'block' }}
                        >
                          标题
                        </Typography>
                        <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>
                          {hit.title || '—'}
                        </Typography>
                      </Box>
                      <Box>
                        <Typography
                          variant="caption"
                          sx={{ color: 'text.secondary', display: 'block', mb: 0.5 }}
                        >
                          正文
                        </Typography>
                        <Box
                          component="pre"
                          sx={{
                            m: 0,
                            p: 1.5,
                            borderRadius: 1.5,
                            bgcolor: 'background.default',
                            border: 1,
                            borderColor: 'divider',
                            fontSize: 13,
                            whiteSpace: 'pre-wrap',
                            wordBreak: 'break-word',
                            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                          }}
                        >
                          {hit.text}
                        </Box>
                      </Box>
                    </Stack>
                  </Box>
                ))
              )}
            </Stack>
            <FeishuPreview
              title={previewTitle}
              card={cardLabel}
              sender={sender}
              text={previewText}
            />
          </Box>
        </Stack>
      </CardContent>
    </Card>
  )
}

function FeishuPreview({
  title,
  card,
  sender,
  text,
}: {
  title: string
  card: string
  sender: string
  text: string
}) {
  return (
    <Box>
      <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 1 }}>
        飞书消息卡片
      </Typography>
      <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 1, overflow: 'hidden' }}>
        <Box sx={{ bgcolor: 'primary.main', color: 'primary.contrastText', px: 2, py: 1.5 }}>
          <Typography variant="h3" sx={{ overflowWrap: 'anywhere' }}>
            {title}
          </Typography>
        </Box>
        <Stack spacing={1.5} sx={{ p: 2 }}>
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 2 }}>
            <Box>
              <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                卡片
              </Typography>
              <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>
                {card}
              </Typography>
            </Box>
            <Box>
              <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                发件人
              </Typography>
              <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>
                {sender}
              </Typography>
            </Box>
          </Box>
          <Divider />
          <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
            {text}
          </Typography>
          <Divider />
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            {new Date().toLocaleString('zh-CN', { hour12: false })}
          </Typography>
        </Stack>
      </Box>
    </Box>
  )
}

function DeliveryLogCard({ logs }: { logs: NotifyLog[] }) {
  return (
    <Card>
      <CardHeader title={<Typography variant="h3">最近投递</Typography>} />
      <CardContent sx={{ p: 0, '&:last-child': { pb: 0 } }}>
        {logs.length === 0 ? (
          <Typography variant="body2" sx={{ color: 'text.secondary', px: 2, pb: 2 }}>
            还没有投递记录
          </Typography>
        ) : (
          <TableContainer>
            <Table size="small" sx={{ minWidth: 560 }}>
              <TableHead>
                <TableRow>
                  <TableCell>时间</TableCell>
                  <TableCell>渠道</TableCell>
                  <TableCell>结果</TableCell>
                  <TableCell>尝试</TableCell>
                  <TableCell>详情</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {logs.map((log) => (
                  <TableRow key={log.id}>
                    <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(log.ts)}</TableCell>
                    <TableCell>{log.channel_name || log.channel_id || '系统通知'}</TableCell>
                    <TableCell sx={{ color: log.status === 'ok' ? STATUS.good : STATUS.critical }}>
                      {log.status === 'ok' ? '成功' : '失败'}
                    </TableCell>
                    <TableCell>{log.attempts}</TableCell>
                    <TableCell>{log.detail || '—'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </CardContent>
    </Card>
  )
}

function SettingsCard({
  onSaved,
  onError,
}: {
  onSaved: () => void
  onError: (message: string) => void
}) {
  // Two `null` drafts, meaning "not touched yet". Seeding form state from an
  // effect would fight the fetch: a background revalidation landing mid-edit
  // would silently overwrite what the user typed.
  const [retentionDraft, setRetentionDraft] = useState<string | null>(null)
  const [alertsDraft, setAlertsDraft] = useState<boolean | null>(null)
  const [saving, setSaving] = useState(false)

  const { data: settings, mutate } = useSWR(
    '/api/notify-settings',
    () => api.notifySettings.get(),
    { onError: (err) => onError(err instanceof ApiError ? err.message : '加载设置失败') },
  )

  // Kept as a string so the field can be temporarily empty while typing.
  const retention = retentionDraft ?? String(settings?.message_retention_days ?? '')
  const offlineAlerts = alertsDraft ?? settings?.offline_alerts_enabled ?? false
  const setRetention = setRetentionDraft
  const setOfflineAlerts = setAlertsDraft

  const days = Math.round(Number(retention))
  const daysValid = retention.trim() !== '' && Number.isFinite(days) && days >= 0 && days <= 3650
  const dirty =
    settings !== undefined &&
    (days !== settings.message_retention_days || offlineAlerts !== settings.offline_alerts_enabled)

  const save = async () => {
    if (!daysValid) return
    setSaving(true)
    try {
      const next = await api.notifySettings.update({
        message_retention_days: days,
        offline_alerts_enabled: offlineAlerts,
      })
      await mutate(next, { revalidate: false })
      // Drop the drafts so the fields track the saved server values again.
      setRetentionDraft(null)
      setAlertsDraft(null)
      onSaved()
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card>
      <CardHeader title={<Typography variant="h3">短信与告警</Typography>} />
      <CardContent sx={{ pt: 0 }}>
        {settings === undefined ? (
          <CircularProgress size={20} />
        ) : (
          <Stack spacing={2}>
            <TextField
              label="短信保留天数"
              type="number"
              value={retention}
              onChange={(e) => setRetention(e.target.value)}
              error={!daysValid}
              helperText={
                daysValid && days === 0
                  ? '0 = 永久保留,不自动清理'
                  : daysValid
                    ? `超过 ${days} 天的短信会被服务器自动清理`
                    : '请填 0-3650 之间的天数(0 = 永久保留)'
              }
              sx={{ maxWidth: 260 }}
              slotProps={{
                htmlInput: { min: 0, max: 3650, step: 1 }
              }}
            />
            <Stack direction="row" spacing={1} sx={{
              alignItems: 'center'
            }}>
              <Switch
                checked={offlineAlerts}
                onChange={(e) => setOfflineAlerts(e.target.checked)}
                slotProps={{
                  input: { 'aria-label': '模块掉线告警' }
                }}
              />
              <Typography variant="body2">模块掉线时推送告警</Typography>
            </Stack>
            <div>
              <Button
                variant="contained"
                onClick={() => void save()}
                disabled={!dirty || !daysValid || saving}
              >
                保存
              </Button>
            </div>
          </Stack>
        )}
      </CardContent>
    </Card>
  )
}

function ChannelDialog({
  channel,
  onClose,
  onSaved,
  onError,
}: {
  channel: Channel | null
  onClose: () => void
  onSaved: (created: boolean) => void | Promise<void>
  onError: (message: string) => void
}) {
  const [name, setName] = useState(channel?.name ?? '')
  const [type, setType] = useState(channel?.type ?? 'bark')
  const [values, setValues] = useState<Record<string, string>>(
    channel ? parseConfig(channel) : {},
  )

  const spec = CHANNEL_TYPES.find((t) => t.value === type) ?? CHANNEL_TYPES[0]
  const missing = spec.fields.some((field) => !field.optional && !values[field.key]?.trim())

  const save = async () => {
    // Only send what was filled in — an empty string would look like "signing
    // is on, with a blank secret" to the server.
    const config: Record<string, string> = {}
    spec.fields.forEach((field) => {
      const value = values[field.key]?.trim()
      if (value) config[field.key] = value
    })

    const body: ChannelInput = { name, type, config, enabled: channel ? Boolean(channel.enabled) : true }
    try {
      if (channel) await api.channels.update(channel.id, body)
      else await api.channels.create(body)
      await onSaved(!channel)
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '保存失败')
    }
  }

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>{channel ? '编辑推送渠道' : '新建推送渠道'}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            label="名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
            fullWidth
            placeholder="我的 Bark"
          />
          <TextField
            select
            label="类型"
            value={type}
            onChange={(e) => {
              setType(e.target.value)
              setValues({})
            }}
            fullWidth
          >
            {CHANNEL_TYPES.map((option) => (
              <MenuItem key={option.value} value={option.value}>
                {option.label}
              </MenuItem>
            ))}
          </TextField>
          {spec.fields.map((field) => (
            <TextField
              key={field.key}
              label={field.optional ? `${field.label}(可选)` : field.label}
              value={values[field.key] ?? ''}
              onChange={(e) => setValues((v) => ({ ...v, [field.key]: e.target.value }))}
              helperText={field.hint}
              type={field.password ? 'password' : 'text'}
              fullWidth
            />
          ))}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>取消</Button>
        <Button variant="contained" onClick={save} disabled={!name || missing}>
          {channel ? '保存' : '创建'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}

function ruleToInput(rule: Rule): RuleInput {
  return {
    name: rule.name,
    sim_id: rule.sim_id,
    channel_id: rule.channel_id,
    match: rule.match,
    pattern: rule.pattern,
    template: rule.template,
    priority: rule.priority,
    enabled: Boolean(rule.enabled),
  }
}

function RuleDialog({
  rule,
  channels,
  sims,
  onClose,
  onSaved,
  onError,
}: {
  rule: Rule | null
  channels: Channel[]
  sims: Sim[]
  onClose: () => void
  onSaved: (created: boolean) => void | Promise<void>
  onError: (message: string) => void
}) {
  const [name, setName] = useState(rule?.name ?? '')
  const [channelId, setChannelId] = useState(rule?.channel_id ?? channels[0]?.id ?? 0)
  const [simId, setSimId] = useState<number | ''>(rule?.sim_id ?? '')
  const [match, setMatch] = useState<Rule['match']>(rule?.match ?? 'all')
  const [pattern, setPattern] = useState(rule?.pattern ?? '')
  const [template, setTemplate] = useState(rule?.template ?? '')
  const [priority, setPriority] = useState(String(rule?.priority ?? 0))

  const save = async () => {
    const body: RuleInput = {
      name,
      channel_id: channelId,
      sim_id: simId === '' ? null : simId,
      match,
      pattern,
      template,
      priority: Number(priority) || 0,
      enabled: rule ? Boolean(rule.enabled) : true,
    }
    try {
      if (rule) await api.rules.update(rule.id, body)
      else await api.rules.create(body)
      await onSaved(!rule)
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '保存失败')
    }
  }

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>{rule ? '编辑转发规则' : '新建转发规则'}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            label="名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
            fullWidth
            placeholder="验证码转 Bark"
          />
          <TextField
            select
            label="适用的卡"
            value={simId}
            onChange={(e) => setSimId(e.target.value === '' ? '' : Number(e.target.value))}
            fullWidth
          >
            <MenuItem value="">全部卡</MenuItem>
            {sims.map((sim) => (
              <MenuItem key={sim.id} value={sim.id}>
                {sim.label || sim.iccid}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            select
            label="匹配方式"
            value={match}
            onChange={(e) => setMatch(e.target.value as Rule['match'])}
            fullWidth
          >
            <MenuItem value="all">全部短信</MenuItem>
            <MenuItem value="keyword">包含关键词</MenuItem>
            <MenuItem value="regex">正则匹配</MenuItem>
          </TextField>
          {match !== 'all' && (
            <TextField
              label={match === 'keyword' ? '关键词' : '正则表达式'}
              value={pattern}
              onChange={(e) => setPattern(e.target.value)}
              fullWidth
              placeholder={match === 'keyword' ? '验证码' : '\\d{4,6}'}
            />
          )}
          <TextField
            select
            label="推送到"
            value={channelId}
            onChange={(e) => setChannelId(Number(e.target.value))}
            fullWidth
          >
            {channels.map((channel) => (
              <MenuItem key={channel.id} value={channel.id}>
                {channel.name}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="消息模板(可选)"
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
            helperText={`留空使用默认「【{card}】{sender} + 正文」。${TEMPLATE_HINT}`}
            fullWidth
            multiline
            minRows={2}
          />
          <TextField
            label="优先级"
            value={priority}
            onChange={(e) => setPriority(e.target.value)}
            helperText="同一渠道被多条规则命中时,数字大的那条决定模板"
            fullWidth
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>取消</Button>
        <Button
          variant="contained"
          onClick={save}
          disabled={!channelId || (match !== 'all' && !pattern)}
        >
          {rule ? '保存' : '创建'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
