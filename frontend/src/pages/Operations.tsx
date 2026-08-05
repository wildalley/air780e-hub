import { useCallback, useEffect, useState } from 'react'
import {
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
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
import StorageIcon from '@mui/icons-material/StorageOutlined'
import { api, ApiError, type AuditEvent, type Diagnostics, type Incident } from '../api'
import { Loading, OnlineChip, formatTs, useToast } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { StatTile } from '../components/StatTile'
import { STATUS } from '../tokens'

const REFRESH_MS = 15_000

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

export function OperationsPage() {
  const toast = useToast()
  const [tab, setTab] = useState(0)
  const [scope, setScope] = useState<'open' | 'all'>('open')
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null)
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [busyId, setBusyId] = useState<number | null>(null)

  const load = useCallback(async () => {
    const [nextDiagnostics, nextIncidents, nextAudit] = await Promise.all([
      api.operations.diagnostics(),
      api.operations.incidents(scope),
      api.operations.audit(),
    ])
    setDiagnostics(nextDiagnostics)
    setIncidents(nextIncidents)
    setAudit(nextAudit)
  }, [scope])

  useEffect(() => {
    void load().catch(() => {})
    const timer = setInterval(() => void load().catch(() => {}), REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

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

  if (diagnostics === null) return <Loading />

  const diskUsed = diagnostics.storage.disk_total_bytes - diagnostics.storage.disk_free_bytes
  const diskPercent = diagnostics.storage.disk_total_bytes
    ? Math.round((diskUsed / diagnostics.storage.disk_total_bytes) * 100)
    : 0

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

      <Card>
        <CardHeader
          title={<Typography variant="h3">主机与 Agent</Typography>}
          action={<Chip size="small" label={`磁盘已用 ${diskPercent}%`} variant="outlined" />}
        />
        <CardContent sx={{ pt: 0 }}>
          <Stack spacing={2}>
            <Box>
              <LinearProgress
                variant="determinate"
                value={Math.min(100, diskPercent)}
                color={diskPercent >= 90 ? 'error' : diskPercent >= 75 ? 'warning' : 'primary'}
                sx={{ height: 7, borderRadius: 1 }}
              />
              <Typography variant="caption" color="text.secondary" sx={{ mt: 0.75, display: 'block' }}>
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
                    <TableRow>
                      <TableCell colSpan={6} align="center" sx={{ py: 4, color: 'text.secondary' }}>
                        尚无 Agent 记录
                      </TableCell>
                    </TableRow>
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
              <Typography variant="caption" color="text.secondary">
                待处理命令 {diagnostics.runtime.pending_commands}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                发送中的通知 {diagnostics.runtime.notifications_inflight}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                离线观察计时器 {diagnostics.runtime.offline_timers}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                状态采样 {diagnostics.counts.status_samples.toLocaleString()}
              </Typography>
            </Stack>
          </Stack>
        </CardContent>
      </Card>

      <Card>
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
                  onChange={(_, value) => value && setScope(value)}
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
                      <TableRow>
                        <TableCell colSpan={7} align="center" sx={{ py: 5, color: 'text.secondary' }}>
                          当前没有事件
                        </TableCell>
                      </TableRow>
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
                            <Typography variant="body2" fontWeight={600}>{incident.title}</Typography>
                            {incident.detail && (
                              <Typography variant="caption" color="text.secondary">
                                {incident.detail}
                              </Typography>
                            )}
                          </TableCell>
                          <TableCell>{incident.source || '—'}</TableCell>
                          <TableCell>{STATUS_LABEL[incident.status]}</TableCell>
                          <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(incident.last_seen_at)}</TableCell>
                          <TableCell>{incident.occurrences}</TableCell>
                          <TableCell align="right">
                            <Stack direction="row" spacing={1} justifyContent="flex-end">
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
                    <TableRow>
                      <TableCell colSpan={6} align="center" sx={{ py: 5, color: 'text.secondary' }}>
                        还没有管理操作记录
                      </TableCell>
                    </TableRow>
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
            </TableContainer>
          )}
        </CardContent>
      </Card>
      {toast.element}
    </Stack>
  )
}
