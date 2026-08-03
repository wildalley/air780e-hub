import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
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
  IconButton,
  MenuItem,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import DeleteIcon from '@mui/icons-material/DeleteOutline'
import EditIcon from '@mui/icons-material/EditOutlined'
import SendIcon from '@mui/icons-material/SendOutlined'
import {
  api,
  ApiError,
  type Channel,
  type ChannelInput,
  type Rule,
  type RuleInput,
  type Sim,
} from '../api'
import { Loading, useToast } from '../components/common'
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
  const [channels, setChannels] = useState<Channel[] | null>(null)
  const [rules, setRules] = useState<Rule[]>([])
  const [sims, setSims] = useState<Sim[]>([])
  const [channelEdit, setChannelEdit] = useState<Channel | null | undefined>(undefined)
  const [ruleEdit, setRuleEdit] = useState<Rule | null | undefined>(undefined)
  const [testing, setTesting] = useState<number | null>(null)

  const load = useCallback(async () => {
    const [channelList, ruleList, simList] = await Promise.all([
      api.channels.list(),
      api.rules.list(),
      api.sims.list(),
    ])
    setChannels(channelList)
    setRules(ruleList)
    setSims(simList)
  }, [])

  useEffect(() => {
    void load()
  }, [load])

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

  if (channels === null) return <Loading />

  return (
    <Stack spacing={3}>
      <Typography variant="h1">通知</Typography>

      <Alert severity="info">
        推送由 <strong>服务器</strong> 发出,走机房网络 —— 不消耗 SIM 卡流量,所以纯保号卡也能用。
      </Alert>

      <Card>
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
            <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
              还没有渠道。先建一个,再用规则把短信路由过去。
            </Typography>
          ) : (
            <Table size="small">
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
                        inputProps={{ 'aria-label': `启用 ${channel.name}` }}
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
          )}
        </CardContent>
      </Card>

      <Card>
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
            <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
              还没有规则。没有规则时不会推送任何短信。
            </Typography>
          ) : (
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>名称</TableCell>
                  <TableCell>范围</TableCell>
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
                    <TableCell>
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
                        inputProps={{ 'aria-label': `启用规则 ${rule.id}` }}
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
          )}
        </CardContent>
      </Card>

      <Typography variant="caption" color="text.secondary">
        一条短信命中多条规则时,同一个渠道只推一次(取优先级最高的模板)。推送结果见「日志 →
        推送日志」。
      </Typography>

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
