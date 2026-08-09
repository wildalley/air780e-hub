import { Fragment, useCallback, useState } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  LinearProgress,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tabs,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material'
import AccessTimeIcon from '@mui/icons-material/AccessTimeOutlined'
import AgentIcon from '@mui/icons-material/HubOutlined'
import AcknowledgeIcon from '@mui/icons-material/DoneAllOutlined'
import IncidentIcon from '@mui/icons-material/ErrorOutlineOutlined'
import ResolveIcon from '@mui/icons-material/CheckCircleOutlineOutlined'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import StorageIcon from '@mui/icons-material/StorageOutlined'
import useSWR, { mutate as mutateKey } from 'swr'
import { successRate } from '../opsStats'
import { api, ApiError, type ActivityWindow, type Incident } from '../api'
import { formatTs } from '../format'
import { usePager } from '../swr'
import { useToast } from '../toast'
import { EmptyRow, Loading, OnlineChip, Pager } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { StatTile } from '../components/StatTile'
import { LIVE_MS } from '../swr'
import { STATUS } from '../tokens'

const SEVERITY_LABEL: Record<Incident['severity'], string> = {
  critical: '严重',
  warning: '警告',
  info: '提示',
}

const STATUS_LABEL: Record<Incident['status'], string> = {
  active: '待处理',
  acknowledged: '已确认',
  resolved: '已解决',
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let amount = value / 1024
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[unit]}`
}

function formatDuration(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days) return `${days} 天 ${hours} 小时`
  if (hours) return `${hours} 小时 ${minutes} 分`
  return `${minutes} 分钟`
}

function severityColor(severity: Incident['severity']): string {
  if (severity === 'critical') return STATUS.critical
  if (severity === 'warning') return STATUS.warning
  return 'text.secondary'
}

/** Row counts, biggest first — the interesting one is whichever is growing. */
const ROW_LABEL: Record<string, string> = {
  messages: '短信',
  device_status: '状态采样',
  notify_logs: '通知日志',
  task_logs: '任务日志',
  agent_logs: 'Agent 日志',
  audit_events: '管理审计',
  incidents: '事件',
  ingested: '幂等记录',
  sims: '卡',
  devices: '模块',
  channels: '渠道',
  rules: '规则',
  tasks: '任务',
}

/** One labelled 24h / 7d pair. */
function WindowStat({ label, window, accent }: {
  label: string
  window: ActivityWindow
  accent?: string
}) {
  return (
    <Box>
      <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ color: accent ?? 'text.primary', fontWeight: 600 }}>
        {window.day.toLocaleString()}
        <Typography component="span" variant="caption" sx={{ color: 'text.secondary', ml: 0.75 }}>
          / {window.week.toLocaleString()}
        </Typography>
      </Typography>
    </Box>
  )
}

export function OperationsPage() {
  const toast = useToast()
  const [tab, setTab] = useState(0)
  const [scope, setScope] = useState<'open' | 'all'>('open')
  const [busyId, setBusyId] = useState<number | null>(null)

  const { data: diagnostics, mutate: mutateDiagnostics } = useSWR(
    '/api/operations/diagnostics',
    () => api.operations.diagnostics(),
    { refreshInterval: LIVE_MS },
  )
  // Scope in the key: flipping 未解决/全部 serves the other list from cache and
  // leaves diagnostics and the audit log untouched.
  const incidentPager = usePager()
  const auditPager = usePager()

  const { data: incidentPage, mutate: mutateIncidents } = useSWR(
    ['/api/operations/incidents', scope, incidentPager.query],
    () => api.operations.incidents(scope, incidentPager.query),
    { refreshInterval: LIVE_MS, keepPreviousData: true },
  )
  const { data: auditPage } = useSWR(
    ['/api/operations/audit', auditPager.query],
    () => api.operations.audit(auditPager.query),
    { refreshInterval: LIVE_MS, keepPreviousData: true },
  )
  const incidents = incidentPage?.items ?? []
  const audit = auditPage?.items ?? []

  // Resolving an incident changes the list, the active-incident tile in
  // diagnostics, and the nav badge that reads the same count endpoint.
  const load = useCallback(
    () =>
      Promise.all([
        mutateIncidents(),
        mutateDiagnostics(),
        // Owned by Layout, which is not in this tree — revalidate by key so the
        // badge clears with the row instead of lingering for up to 15 s.
        mutateKey('/api/operations/incidents/count'),
      ]),
    [mutateIncidents, mutateDiagnostics],
  )

  const updateIncident = async (id: number, status: Incident['status']) => {
    setBusyId(id)
    try {
      await api.operations.setIncidentStatus(id, status)
      await load()
      toast.show(status === 'resolved' ? '事件已解决' : '事件已确认', 'success')
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : '更新事件失败', 'error')
    } finally {
      setBusyId(null)
    }
  }

  if (!diagnostics) return <Loading />

  const diskUsed = diagnostics.storage.disk_total_bytes - diagnostics.storage.disk_free_bytes
  const diskPercent = diagnostics.storage.disk_total_bytes
    ? Math.round((diskUsed / diagnostics.storage.disk_total_bytes) * 100)
    : 0

  const activity = diagnostics.activity
  // Outbound only: a received message has no send outcome to succeed at.
  const smsRate = successRate(
    activity.messages.outbound.day - activity.messages.failed.day,
    activity.messages.failed.day,
  )
  const notifyRate = successRate(
    activity.notifications.ok.day,
    activity.notifications.failed.day,
  )
  // Tasks run on a multi-day cycle by design, so 24h is usually an empty
  // window — the 7d rate is the one that carries information.
  const taskRate = successRate(activity.tasks.ok.week, activity.tasks.failed.week)
  const tableRows = Object.entries(activity.rows).sort((a, b) => b[1] - a[1])

  return (
    <Stack spacing={3}>
      <PageHeader title="运维中心" subtitle="运行状态、事件与管理审计" />

      <Box
        sx={{
          display: 'grid',
          gap: 2,
          gridTemplateColumns: { xs: 'repeat(2, 1fr)', lg: 'repeat(4, 1fr)' },
        }}
      >
        <StatTile
          label="运行时间"
          value={formatDuration(diagnostics.server.uptime_seconds)}
          note={`Server ${diagnostics.server.version} · Python ${diagnostics.server.python}`}
          icon={<AccessTimeIcon />}
        />
        <StatTile
          label="Agent 在线"
          value={`${diagnostics.runtime.agents_connected} / ${diagnostics.agents.length}`}
          accent={
            diagnostics.runtime.agents_connected === diagnostics.agents.length
              ? STATUS.good
              : STATUS.critical
          }
          icon={<AgentIcon />}
        />
        <StatTile
          label="未解决事件"
          value={diagnostics.counts.active_incidents}
          accent={diagnostics.counts.active_incidents ? STATUS.critical : STATUS.good}
          icon={<IncidentIcon />}
        />
        <StatTile
          label="数据库"
          value={formatBytes(diagnostics.storage.database_bytes)}
          note={`WAL ${formatBytes(diagnostics.storage.wal_bytes)}`}
          icon={<StorageIcon />}
        />
      </Box>

      <Accordion disableGutters sx={{ order: 2 }}>
        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
          <Stack
            direction="row"
            spacing={2}
            sx={{ alignItems: 'center', justifyContent: 'space-between', width: '100%', pr: 1 }}
          >
            <Typography variant="h3">主机与 Agent</Typography>
            <Chip size="small" label={`磁盘已用 ${diskPercent}%`} variant="outlined" />
          </Stack>
        </AccordionSummary>
        <AccordionDetails sx={{ pt: 0 }}>
          <Stack spacing={2}>
            <Box>
              <LinearProgress
                variant="determinate"
                value={Math.min(100, diskPercent)}
                color={diskPercent >= 90 ? 'error' : diskPercent >= 75 ? 'warning' : 'primary'}
                sx={{ height: 7, borderRadius: 1 }}
              />
              <Typography
                variant="caption"
                sx={{
                  color: 'text.secondary',
                  mt: 0.75,
                  display: 'block'
                }}>
                {formatBytes(diskUsed)} / {formatBytes(diagnostics.storage.disk_total_bytes)}，可用{' '}
                {formatBytes(diagnostics.storage.disk_free_bytes)}
              </Typography>
            </Box>

            <TableContainer>
              <Table size="small" sx={{ minWidth: 620 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>Agent</TableCell>
                    <TableCell>状态</TableCell>
                    <TableCell>版本</TableCell>
                    <TableCell>设备</TableCell>
                    <TableCell>最后上报</TableCell>
                    <TableCell>已确认序号</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {diagnostics.agents.length === 0 ? (
                    <EmptyRow colSpan={6}>尚无 Agent 记录</EmptyRow>
                  ) : (
                    diagnostics.agents.map((agent) => (
                      <TableRow key={agent.id}>
                        <TableCell>{agent.id}</TableCell>
                        <TableCell><OnlineChip online={Boolean(agent.connected)} /></TableCell>
                        <TableCell>{agent.version || '—'}</TableCell>
                        <TableCell>{agent.device_count}</TableCell>
                        <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(agent.last_seen_at)}</TableCell>
                        <TableCell>{agent.last_seq}</TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </TableContainer>

            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={3}>
              <Typography variant="caption" sx={{
                color: 'text.secondary'
              }}>
                待处理命令 {diagnostics.runtime.pending_commands}
              </Typography>
              <Typography variant="caption" sx={{
                color: 'text.secondary'
              }}>
                发送中的通知 {diagnostics.runtime.notifications_inflight}
              </Typography>
              <Typography variant="caption" sx={{
                color: 'text.secondary'
              }}>
                离线观察计时器 {diagnostics.runtime.offline_timers}
              </Typography>
              <Typography variant="caption" sx={{
                color: 'text.secondary'
              }}>
                状态采样 {diagnostics.counts.status_samples.toLocaleString()}
              </Typography>
            </Stack>
          </Stack>
        </AccordionDetails>
      </Accordion>

      <Accordion disableGutters sx={{ order: 2 }}>
        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
          <Box>
            <Typography variant="h3">运行统计</Typography>
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
              每格为「最近 24 小时 / 最近 7 天」
            </Typography>
          </Box>
        </AccordionSummary>
        <AccordionDetails sx={{ pt: 0 }}>
          <Stack spacing={2.5}>
            <Box
              sx={{
                display: 'grid',
                gap: 2,
                gridTemplateColumns: { xs: 'repeat(2, 1fr)', sm: 'repeat(4, 1fr)' },
              }}
            >
              <WindowStat label="收到短信" window={activity.messages.inbound} />
              <WindowStat label="发出短信" window={activity.messages.outbound} />
              <WindowStat
                label="发送失败"
                window={activity.messages.failed}
                accent={activity.messages.failed.day ? STATUS.critical : undefined}
              />
              <Box>
                <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                  短信成功率（24h）
                </Typography>
                <Typography
                  variant="body2"
                  sx={{ color: smsRate.accent ?? 'text.primary', fontWeight: 600 }}
                >
                  {smsRate.text}
                </Typography>
              </Box>
            </Box>

            <Box
              sx={{
                display: 'grid',
                gap: 2,
                gridTemplateColumns: { xs: 'repeat(2, 1fr)', sm: 'repeat(4, 1fr)' },
              }}
            >
              <WindowStat label="通知成功" window={activity.notifications.ok} />
              <WindowStat
                label="通知失败"
                window={activity.notifications.failed}
                accent={activity.notifications.failed.day ? STATUS.critical : undefined}
              />
              <Box>
                <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                  通知成功率（24h）
                </Typography>
                <Typography
                  variant="body2"
                  sx={{ color: notifyRate.accent ?? 'text.primary', fontWeight: 600 }}
                >
                  {notifyRate.text}
                </Typography>
              </Box>
              <Box>
                <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                  任务成功率（7d）
                </Typography>
                <Typography
                  variant="body2"
                  sx={{ color: taskRate.accent ?? 'text.primary', fontWeight: 600 }}
                >
                  {taskRate.text}
                </Typography>
              </Box>
            </Box>

            <Box
              sx={{
                display: 'grid',
                gap: 2,
                gridTemplateColumns: { xs: 'repeat(2, 1fr)', sm: 'repeat(4, 1fr)' },
              }}
            >
              <WindowStat label="任务成功" window={activity.tasks.ok} />
              <WindowStat
                label="任务失败"
                window={activity.tasks.failed}
                accent={activity.tasks.failed.day ? STATUS.critical : undefined}
              />
              <WindowStat label="任务跳过" window={activity.tasks.skipped} />
            </Box>

            <Box>
              <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 1 }}>
                数据表行数
              </Typography>
              <Box
                sx={{
                  display: 'grid',
                  gap: 0.75,
                  gridTemplateColumns: {
                    xs: 'auto 1fr',
                    sm: 'repeat(2, auto 1fr)',
                    lg: 'repeat(3, auto 1fr)',
                  },
                  columnGap: 2,
                }}
              >
                {tableRows.map(([table, count]) => (
                  <Fragment key={table}>
                    <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                      {ROW_LABEL[table] ?? table}
                    </Typography>
                    <Typography
                      variant="caption"
                      sx={{ fontVariantNumeric: 'tabular-nums', textAlign: 'right' }}
                    >
                      {count.toLocaleString()}
                    </Typography>
                  </Fragment>
                ))}
              </Box>
            </Box>
          </Stack>
        </AccordionDetails>
      </Accordion>

      <Card sx={{ order: 1 }}>
        <Tabs value={tab} onChange={(_, next) => setTab(next)} sx={{ px: 2 }}>
          <Tab label={`事件 (${incidents.length})`} />
          <Tab label={`管理审计 (${audit.length})`} />
        </Tabs>
        <CardContent sx={{ p: 0, '&:last-child': { pb: 0 } }}>
          {tab === 0 ? (
            <>
              <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: 'divider' }}>
                <ToggleButtonGroup
                  exclusive
                  size="small"
                  value={scope}
                  onChange={(_, value) => {
                    if (!value) return
                    setScope(value)
                    // 未解决 holds fewer rows than 全部, so a page that exists in
                    // one may not in the other — narrowing while deep in the
                    // list would otherwise land on a blank page.
                    incidentPager.reset()
                  }}
                  aria-label="事件范围"
                >
                  <ToggleButton value="open">未解决</ToggleButton>
                  <ToggleButton value="all">全部</ToggleButton>
                </ToggleButtonGroup>
              </Box>
              <TableContainer>
                <Table size="small" sx={{ minWidth: 860 }}>
                  <TableHead>
                    <TableRow>
                      <TableCell>级别</TableCell>
                      <TableCell>事件</TableCell>
                      <TableCell>来源</TableCell>
                      <TableCell>状态</TableCell>
                      <TableCell>最近发生</TableCell>
                      <TableCell>次数</TableCell>
                      <TableCell align="right">操作</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {incidents.length === 0 ? (
                      <EmptyRow colSpan={7} py={5}>当前没有事件</EmptyRow>
                    ) : (
                      incidents.map((incident) => (
                        <TableRow key={incident.id} hover>
                          <TableCell>
                            <Chip
                              size="small"
                              variant="outlined"
                              label={SEVERITY_LABEL[incident.severity]}
                              sx={{ color: severityColor(incident.severity), borderColor: 'currentColor' }}
                            />
                          </TableCell>
                          <TableCell sx={{ maxWidth: 380 }}>
                            <Typography variant="body2" sx={{
                              fontWeight: 600
                            }}>{incident.title}</Typography>
                            {incident.detail && (
                              <Typography variant="caption" sx={{
                                color: 'text.secondary'
                              }}>
                                {incident.detail}
                              </Typography>
                            )}
                          </TableCell>
                          <TableCell>{incident.source || '—'}</TableCell>
                          <TableCell>{STATUS_LABEL[incident.status]}</TableCell>
                          <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(incident.last_seen_at)}</TableCell>
                          <TableCell>{incident.occurrences}</TableCell>
                          <TableCell align="right">
                            <Stack direction="row" spacing={1} sx={{
                              justifyContent: 'flex-end'
                            }}>
                              {incident.status === 'active' && (
                                <Button
                                  size="small"
                                  startIcon={<AcknowledgeIcon />}
                                  disabled={busyId === incident.id}
                                  onClick={() => void updateIncident(incident.id, 'acknowledged')}
                                >
                                  确认
                                </Button>
                              )}
                              {incident.status !== 'resolved' && (
                                <Button
                                  size="small"
                                  color="success"
                                  startIcon={<ResolveIcon />}
                                  disabled={busyId === incident.id}
                                  onClick={() => void updateIncident(incident.id, 'resolved')}
                                >
                                  解决
                                </Button>
                              )}
                            </Stack>
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
                <Pager total={incidentPage?.total ?? 0} pager={incidentPager} />
              </TableContainer>
            </>
          ) : (
            <TableContainer>
              <Table size="small" sx={{ minWidth: 780 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>时间</TableCell>
                    <TableCell>操作</TableCell>
                    <TableCell>目标</TableCell>
                    <TableCell>结果</TableCell>
                    <TableCell>来源 IP</TableCell>
                    <TableCell>详情</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {audit.length === 0 ? (
                    <EmptyRow colSpan={6} py={5}>还没有管理操作记录</EmptyRow>
                  ) : (
                    audit.map((event) => (
                      <TableRow key={event.id} hover>
                        <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(event.ts)}</TableCell>
                        <TableCell>{event.action}</TableCell>
                        <TableCell>{event.target || '—'}</TableCell>
                        <TableCell sx={{ color: event.status === 'ok' ? STATUS.good : STATUS.critical }}>
                          {event.status === 'ok' ? '成功' : '拒绝'}
                        </TableCell>
                        <TableCell>{event.client_ip || '—'}</TableCell>
                        <TableCell>{event.detail}</TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
              <Pager total={auditPage?.total ?? 0} pager={auditPager} />
            </TableContainer>
          )}
        </CardContent>
      </Card>
      {toast.element}
    </Stack>
  )
}
