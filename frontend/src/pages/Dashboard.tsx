import { useMemo, useState } from 'react'
import { Link as RouterLink } from 'react-router'
import {
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  CircularProgress,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import RouterIcon from '@mui/icons-material/RouterOutlined'
import TodayIcon from '@mui/icons-material/MarkEmailUnreadOutlined'
import AllSmsIcon from '@mui/icons-material/MailOutlined'
import TaskIcon from '@mui/icons-material/EventRepeatOutlined'
import useSWR from 'swr'
import { api, type MessageStat } from '../api'
import { StatTile } from '../components/StatTile'
import { StorageMeter } from '../components/StorageMeter'
import { SignalChart, type SignalSeries } from '../components/SignalChart'
import { Loading, OnlineChip, entranceStyle, formatTs, relativeTs } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { LIVE_MS } from '../swr'
import { STATUS, VIZ, seriesColor } from '../tokens'
import { useTheme } from '@mui/material/styles'
import type { Mode } from '../tokens'

/** The trend chart moves on the scale of days — it does not need 15 s. */
const TREND_MS = 60_000

export function DashboardPage() {
  const theme = useTheme()
  const mode = theme.palette.mode as Mode
  const viz = VIZ[mode]
  const [hours, setHours] = useState(24)
  const [statsDays, setStatsDays] = useState(30)

  const { data: overview } = useSWR('/api/overview', () => api.overview(), {
    // Revalidation holds the previous render rather than flashing a skeleton.
    refreshInterval: LIVE_MS,
  })
  // Separate key per window, so flipping 24h/7d serves an already-fetched
  // range from cache instead of refetching it.
  const { data: history = {} } = useSWR(
    ['/api/devices/history', hours],
    () => api.devices.histories(hours),
    { refreshInterval: LIVE_MS, keepPreviousData: true },
  )
  const { data: stats } = useSWR(
    ['/api/stats/messages', statsDays],
    () => api.stats.messages(statsDays),
    { refreshInterval: TREND_MS, keepPreviousData: true },
  )

  if (!overview) return <Loading />

  const { counters, devices, recent_messages: recent } = overview
  const allOnline = counters.devices_online === counters.devices_total

  // Colour is keyed by the device's stable position in the list, so filtering
  // or a device dropping offline never repaints the other one.
  const series: SignalSeries[] = devices.map((device, index) => ({
    name: device.name,
    label: device.sim_label || device.label || device.name,
    index,
    points: history[device.name] ?? [],
  }))

  return (
    <Stack spacing={3}>
      <PageHeader title="仪表盘" subtitle="模块、短信与存储,一目了然" />

      <Box
        sx={{
          display: 'grid',
          gap: 2,
          gridTemplateColumns: { xs: 'repeat(2, 1fr)', md: 'repeat(4, 1fr)' },
        }}
      >
        <Box sx={entranceStyle(0)}>
          <StatTile
            label="模块在线"
            value={`${counters.devices_online} / ${counters.devices_total}`}
            accent={allOnline ? STATUS.good : STATUS.critical}
            note={allOnline ? '全部在线' : '有模块离线'}
            icon={<RouterIcon />}
            compact={false}
          />
        </Box>
        <Box sx={entranceStyle(60)}>
          <StatTile label="今日短信" value={counters.messages_today} icon={<TodayIcon />} />
        </Box>
        <Box sx={entranceStyle(120)}>
          <StatTile label="短信总数" value={counters.messages_total} icon={<AllSmsIcon />} />
        </Box>
        <Box sx={entranceStyle(180)}>
          <StatTile
            label="启用的保号任务"
            value={counters.tasks_enabled}
            note={
              counters.tasks_enabled === 0 ? (
                <Box component={RouterLink} to="/tasks" sx={{ color: 'inherit' }}>
                  还没有配置 →
                </Box>
              ) : undefined
            }
            icon={<TaskIcon />}
          />
        </Box>
      </Box>

      <Box sx={entranceStyle(220)}>
        <SignalChart series={series} hours={hours} onHoursChange={setHours} />
      </Box>

      <Box sx={entranceStyle(240)}>
        <TrendCard stats={stats ?? null} days={statsDays} onDaysChange={setStatsDays} viz={viz} />
      </Box>

      <Box
        sx={{
          display: 'grid',
          gap: 2,
          gridTemplateColumns: { xs: '1fr', md: 'repeat(2, 1fr)' },
        }}
      >
        {devices.map((device, index) => (
          <Box key={device.id} sx={entranceStyle(260 + index * 60)}>
            <Card
              sx={{
                height: '100%',
                transition: `box-shadow 200ms ${theme.transitions.easing.easeInOut}, transform 200ms ${theme.transitions.easing.easeInOut}`,
                '&:hover': {
                  boxShadow: viz.shadowHover,
                  transform: 'translateY(-1px)',
                },
              }}
            >
              <CardHeader
                title={
                  <Stack direction="row" spacing={1} sx={{
                    alignItems: 'center'
                  }}>
                    <Box
                      aria-hidden
                      sx={{
                        width: 10,
                        height: 10,
                        borderRadius: '50%',
                        bgcolor: seriesColor(mode, index),
                      }}
                    />
                    <Typography variant="h3">
                      {device.sim_label || device.label || device.name}
                    </Typography>
                  </Stack>
                }
                subheader={
                  <Typography variant="caption" sx={{
                    color: 'text.secondary'
                  }}>
                    {device.operator || '未注册'} · {device.iccid || '无卡'}
                  </Typography>
                }
                action={<OnlineChip online={Boolean(device.online)} />}
              />
              <CardContent>
                <Stack spacing={2}>
                  <Stack direction="row" spacing={3}>
                    <Box>
                      <Typography
                        variant="caption"
                        sx={{
                          color: 'text.secondary',
                          display: 'block'
                        }}>
                        信号
                      </Typography>
                      <Typography sx={{ fontVariantNumeric: 'tabular-nums' }}>
                        {device.dbm != null ? `${device.dbm} dBm` : '—'}
                      </Typography>
                    </Box>
                    <Box>
                      <Typography
                        variant="caption"
                        sx={{
                          color: 'text.secondary',
                          display: 'block'
                        }}>
                        注册
                      </Typography>
                      <Typography>{device.registered ? '已注册' : '未注册'}</Typography>
                    </Box>
                    <Box>
                      <Typography
                        variant="caption"
                        sx={{
                          color: 'text.secondary',
                          display: 'block'
                        }}>
                        最后上报
                      </Typography>
                      <Typography>{relativeTs(device.last_seen_at)}</Typography>
                    </Box>
                  </Stack>
                  <StorageMeter used={device.storage_used} capacity={device.storage_cap} />
                </Stack>
              </CardContent>
            </Card>
          </Box>
        ))}
      </Box>

      <Box sx={entranceStyle(400)}>
        <Card>
          <CardHeader
            title={<Typography variant="h3">最近短信</Typography>}
            action={
              <Button component={RouterLink} to="/messages" size="small">
                查看全部
              </Button>
            }
          />
          <CardContent sx={{ pt: 0 }}>
            {recent.length === 0 ? (
              <Typography
                variant="body2"
                sx={{
                  color: 'text.secondary',
                  py: 3
                }}>
                还没有短信
              </Typography>
            ) : (
              <TableContainer>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>时间</TableCell>
                      <TableCell>卡</TableCell>
                      <TableCell>对方</TableCell>
                      <TableCell>内容</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {recent.map((message) => (
                      <TableRow key={message.id}>
                        <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(message.ts)}</TableCell>
                        <TableCell>{message.sim_label || message.device}</TableCell>
                        <TableCell>{message.peer}</TableCell>
                        <TableCell
                          sx={{
                            maxWidth: 420,
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {message.body}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
            )}
          </CardContent>
        </Card>
      </Box>
    </Stack>
  )
}

/**
 * Message volume over time — stacked received/sent per day.
 *
 * Zero days are filled so a quiet stretch reads as a flat baseline rather
 * than a gap in the axis.
 */
function TrendCard({
  stats,
  days,
  onDaysChange,
  viz,
}: {
  stats: MessageStat[] | null
  days: number
  onDaysChange: (days: number) => void
  viz: (typeof VIZ)[Mode]
}) {
  const data = useMemo(() => {
    const byDay = new Map<string, { day: string; received: number; sent: number }>()
    for (const row of stats ?? []) {
      const entry = byDay.get(row.day) ?? { day: row.day, received: 0, sent: 0 }
      entry.received += row.received
      entry.sent += row.sent
      byDay.set(row.day, entry)
    }
    const out: { day: string; received: number; sent: number }[] = []
    const today = new Date()
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(today)
      d.setDate(today.getDate() - i)
      const key = d.toISOString().slice(0, 10)
      out.push(byDay.get(key) ?? { day: key, received: 0, sent: 0 })
    }
    return out
  }, [stats, days])

  const shortDay = (iso: string) => {
    const d = new Date(`${iso}T00:00:00`)
    return `${d.getMonth() + 1}/${d.getDate()}`
  }

  return (
    <Card>
      <CardHeader
        title={<Typography variant="h3">短信趋势</Typography>}
        action={
          <ToggleButtonGroup
            size="small"
            exclusive
            value={days}
            onChange={(_, next) => next && onDaysChange(next)}
          >
            <ToggleButton value={7}>7 天</ToggleButton>
            <ToggleButton value={30}>30 天</ToggleButton>
            <ToggleButton value={90}>90 天</ToggleButton>
          </ToggleButtonGroup>
        }
      />
      <CardContent>
        {stats === null ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
            <CircularProgress size={24} />
          </Box>
        ) : data.every((d) => d.received === 0 && d.sent === 0) ? (
          <Typography
            variant="body2"
            sx={{
              color: 'text.secondary',
              py: 3,
              textAlign: 'center'
            }}>
            这段时间还没有短信
          </Typography>
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={data} margin={{ top: 4, right: 4, left: -18, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={viz.gridline} vertical={false} />
              <XAxis
                dataKey="day"
                tick={{ fontSize: 11, fill: viz.axis }}
                tickFormatter={shortDay}
                interval="preserveStartEnd"
                minTickGap={24}
                stroke={viz.axis}
              />
              <YAxis
                allowDecimals={false}
                tick={{ fontSize: 11, fill: viz.axis }}
                stroke={viz.axis}
              />
              <Tooltip
                cursor={{ fill: viz.gridline, opacity: 0.5 }}
                contentStyle={{
                  background: viz.surface,
                  border: `1px solid ${viz.border}`,
                  borderRadius: 12,
                  fontSize: 13,
                }}
                labelFormatter={(label) => (typeof label === 'string' ? shortDay(label) : '')}
                formatter={(value, name) => [
                  String(value ?? ''),
                  name === 'received' ? '收到' : '发出',
                ]}
              />
              <Bar dataKey="received" stackId="volume" fill={viz.series[0]} />
              <Bar dataKey="sent" stackId="volume" fill={viz.series[1]} radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  )
}
