import { useCallback, useEffect, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import {
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Grid,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material'
import { api, type Overview, type StatusPoint } from '../api'
import { StatTile } from '../components/StatTile'
import { StorageMeter } from '../components/StorageMeter'
import { SignalChart, type SignalSeries } from '../components/SignalChart'
import { Loading, OnlineChip, formatTs, relativeTs } from '../components/common'
import { STATUS, seriesColor } from '../tokens'
import { useTheme } from '@mui/material/styles'
import type { Mode } from '../tokens'

const REFRESH_MS = 15_000

export function DashboardPage() {
  const theme = useTheme()
  const mode = theme.palette.mode as Mode
  const [overview, setOverview] = useState<Overview | null>(null)
  const [history, setHistory] = useState<Record<string, StatusPoint[]>>({})
  const [hours, setHours] = useState(24)

  const load = useCallback(async () => {
    const data = await api.overview()
    setOverview(data)
    const entries = await Promise.all(
      data.devices.map(async (device) => {
        try {
          return [device.name, await api.devices.history(device.name, hours)] as const
        } catch {
          return [device.name, [] as StatusPoint[]] as const
        }
      }),
    )
    setHistory(Object.fromEntries(entries))
  }, [hours])

  useEffect(() => {
    void load()
    // Refetch holds the previous render rather than flashing a skeleton.
    const timer = setInterval(() => void load(), REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

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
      <Typography variant="h1">仪表盘</Typography>

      <Grid container spacing={2}>
        <Grid item xs={6} md={3}>
          <StatTile
            label="模块在线"
            value={`${counters.devices_online} / ${counters.devices_total}`}
            accent={allOnline ? STATUS.good : STATUS.critical}
            note={allOnline ? '全部在线' : '有模块离线'}
            compact={false}
          />
        </Grid>
        <Grid item xs={6} md={3}>
          <StatTile label="今日短信" value={counters.messages_today} />
        </Grid>
        <Grid item xs={6} md={3}>
          <StatTile label="短信总数" value={counters.messages_total} />
        </Grid>
        <Grid item xs={6} md={3}>
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
          />
        </Grid>
      </Grid>

      <SignalChart series={series} hours={hours} onHoursChange={setHours} />

      <Grid container spacing={2}>
        {devices.map((device, index) => (
          <Grid item xs={12} md={6} key={device.id}>
            <Card>
              <CardHeader
                title={
                  <Stack direction="row" spacing={1} alignItems="center">
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
                  <Typography variant="caption" color="text.secondary">
                    {device.operator || '未注册'} · {device.iccid || '无卡'}
                  </Typography>
                }
                action={<OnlineChip online={Boolean(device.online)} />}
              />
              <CardContent>
                <Stack spacing={2}>
                  <Stack direction="row" spacing={3}>
                    <Box>
                      <Typography variant="caption" color="text.secondary" display="block">
                        信号
                      </Typography>
                      <Typography sx={{ fontVariantNumeric: 'tabular-nums' }}>
                        {device.dbm != null ? `${device.dbm} dBm` : '—'}
                      </Typography>
                    </Box>
                    <Box>
                      <Typography variant="caption" color="text.secondary" display="block">
                        注册
                      </Typography>
                      <Typography>{device.registered ? '已注册' : '未注册'}</Typography>
                    </Box>
                    <Box>
                      <Typography variant="caption" color="text.secondary" display="block">
                        最后上报
                      </Typography>
                      <Typography>{relativeTs(device.last_seen_at)}</Typography>
                    </Box>
                  </Stack>
                  <StorageMeter used={device.storage_used} capacity={device.storage_cap} />
                </Stack>
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

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
            <Typography variant="body2" color="text.secondary" sx={{ py: 3 }}>
              还没有短信
            </Typography>
          ) : (
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
          )}
        </CardContent>
      </Card>
    </Stack>
  )
}
