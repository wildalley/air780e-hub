import { useMemo, useState } from 'react'
import {
  Box,
  Card,
  CardContent,
  CardHeader,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip as MuiTooltip,
  Typography,
  useTheme,
} from '@mui/material'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { STATUS, VIZ, seriesColor, type Mode, type VizTokens } from '../tokens'
import type { StatusPoint } from '../api'
import {
  buildRegistrationIntervals,
  formatRegistrationDuration,
  REGISTRATION_LABEL,
  registrationState,
  summarizeRegistration,
  type RegistrationInterval,
  type RegistrationState,
  type RegistrationSummary,
} from '../signalHistory'

export type SignalStatusSample = Pick<StatusPoint, 'ts' | 'online' | 'registered' | 'dbm'>

export interface SignalSeries {
  /**
   * Fleet-unique series identity — the module's row id as a string.
   *
   * It is the recharts `dataKey` and the React key, so it has to be unique
   * across the whole chart: two agents can each have a module named `modem-1`,
   * and keying on the name collapsed both into one line.  `label` is what the
   * reader sees.
   */
  id: string
  label: string
  index: number
  points: StatusPoint[]
  current?: SignalStatusSample
}

interface Props {
  series: SignalSeries[]
  hours: number
  onHoursChange: (hours: number) => void
}

interface Row {
  t: number
  [key: string]: number | string | null
}

interface TimedSample {
  t: number
  sample: SignalStatusSample
}

interface TimelineRow {
  series: SignalSeries
  intervals: RegistrationInterval[]
  summary: RegistrationSummary
  currentState: RegistrationState
}

const RANGES = [
  { value: 6, label: '6 小时' },
  { value: 24, label: '24 小时' },
  { value: 24 * 7, label: '7 天' },
]

const STATES: RegistrationState[] = ['registered', 'unregistered', 'offline', 'unknown']

function formatTime(t: number, hours: number): string {
  const d = new Date(t)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  if (hours <= 24) return `${hh}:${mm}`
  return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`
}

function statusKey(name: string): string {
  return `${name}::registration`
}

function buildSamples(series: SignalSeries[]): Map<string, TimedSample[]> {
  const output = new Map<string, TimedSample[]>()
  for (const item of series) {
    const byTime = new Map<number, SignalStatusSample>()
    for (const point of item.points) {
      const t = new Date(point.ts).getTime()
      if (Number.isFinite(t)) byTime.set(t, point)
    }
    if (item.current) {
      const t = new Date(item.current.ts).getTime()
      if (Number.isFinite(t)) byTime.set(t, item.current)
    }
    output.set(
      item.id,
      [...byTime.entries()]
        .sort(([a], [b]) => a - b)
        .map(([t, sample]) => ({ t, sample })),
    )
  }
  return output
}

/** Merge every module's actual samples onto a shared time axis. */
function buildRows(
  series: SignalSeries[],
  samples: Map<string, TimedSample[]>,
  rangeStart: number,
  rangeEnd: number,
): Row[] {
  const byTime = new Map<number, Row>()
  for (const item of series) {
    for (const { t, sample } of samples.get(item.id) ?? []) {
      if (t < rangeStart || t > rangeEnd) continue
      let row = byTime.get(t)
      if (!row) {
        row = { t }
        byTime.set(t, row)
      }
      row[item.id] = sample.dbm
      row[statusKey(item.id)] = registrationState(sample)
    }
  }
  return [...byTime.values()].sort((a, b) => a.t - b.t)
}

function sampleAt(samples: TimedSample[], time: number): SignalStatusSample | null {
  let low = 0
  let high = samples.length - 1
  let found: SignalStatusSample | null = null
  while (low <= high) {
    const middle = Math.floor((low + high) / 2)
    if (samples[middle].t <= time) {
      found = samples[middle].sample
      low = middle + 1
    } else {
      high = middle - 1
    }
  }
  return found
}

function stateColor(state: RegistrationState, viz: VizTokens): string {
  if (state === 'registered') return STATUS.good
  if (state === 'unregistered') return STATUS.critical
  if (state === 'offline') return viz.muted
  return viz.gridline
}

function StateKey({ state, viz, line = false }: {
  state: RegistrationState
  viz: VizTokens
  line?: boolean
}) {
  return (
    <Box
      aria-hidden
      sx={{
        width: line ? 16 : 10,
        height: line ? 2 : 10,
        flex: '0 0 auto',
        borderRadius: line ? 1 : 0.75,
        bgcolor: stateColor(state, viz),
        border: state === 'unknown' ? `1px solid ${viz.axis}` : 0,
      }}
    />
  )
}

function summaryText(summary: RegistrationSummary, rangeDuration: number): string {
  const problems: string[] = []
  if (summary.unregisteredCount) {
    problems.push(
      `未注册 ${summary.unregisteredCount} 次 · 约 ${formatRegistrationDuration(summary.unregisteredMs)}`,
    )
  }
  if (summary.offlineCount) {
    problems.push(
      `离线 ${summary.offlineCount} 次 · 约 ${formatRegistrationDuration(summary.offlineMs)}`,
    )
  }
  if (problems.length) return problems.join('；')
  if (summary.unknownMs >= rangeDuration) return '暂无状态数据'
  if (summary.unknownMs > 0) return '已知时段未发现异常'
  return '未发现未注册或离线'
}

function RegistrationTimeline({
  rows,
  rangeStart,
  rangeEnd,
  hours,
  mode,
  viz,
}: {
  rows: TimelineRow[]
  rangeStart: number
  rangeEnd: number
  hours: number
  mode: Mode
  viz: VizTokens
}) {
  const duration = rangeEnd - rangeStart

  return (
    <Box sx={{ mt: 3 }}>
      <Stack
        direction={{ xs: 'column', sm: 'row' }}
        spacing={1}
        sx={{ mb: 1.5, justifyContent: 'space-between', alignItems: { sm: 'center' } }}
      >
        <Typography variant="h3">网络注册</Typography>
        <Stack direction="row" spacing={2} useFlexGap sx={{ flexWrap: 'wrap', rowGap: 0.5 }}>
          {STATES.map((state) => (
            <Stack key={state} direction="row" spacing={0.75} sx={{ alignItems: 'center' }}>
              <StateKey state={state} viz={viz} />
              <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                {REGISTRATION_LABEL[state]}
              </Typography>
            </Stack>
          ))}
        </Stack>
      </Stack>

      <Stack spacing={1.5}>
        {rows.map((row) => (
          <Box key={row.series.id}>
            <Stack
              direction="row"
              spacing={1}
              useFlexGap
              sx={{ mb: 0.75, alignItems: 'center', flexWrap: 'wrap' }}
            >
              <Box
                aria-hidden
                sx={{
                  width: 14,
                  height: 2,
                  flex: '0 0 auto',
                  bgcolor: seriesColor(mode, row.series.index),
                  borderRadius: 1,
                }}
              />
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                {row.series.label}
              </Typography>
              <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center' }}>
                <StateKey state={row.currentState} viz={viz} />
                <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                  {REGISTRATION_LABEL[row.currentState]}
                </Typography>
              </Stack>
              <Typography
                variant="caption"
                sx={{ color: row.summary.unregisteredCount ? STATUS.critical : 'text.secondary' }}
              >
                {summaryText(row.summary, duration)}
              </Typography>
            </Stack>

            <Box
              role="img"
              aria-label={`${row.series.label}：${summaryText(row.summary, duration)}`}
              sx={{
                position: 'relative',
                height: 12,
                ml: '52px',
                mr: '16px',
                overflow: 'hidden',
                borderRadius: 0.75,
                bgcolor: viz.gridline,
                border: `1px solid ${viz.border}`,
              }}
            >
              {row.intervals.map((interval) => {
                const left = ((interval.start - rangeStart) / duration) * 100
                const width = ((interval.end - interval.start) / duration) * 100
                const label = `${REGISTRATION_LABEL[interval.state]}，${formatTime(interval.start, hours)} 至 ${formatTime(interval.end, hours)}`
                return (
                  <MuiTooltip key={`${interval.start}-${interval.state}`} title={label} arrow>
                    <Box
                      component="span"
                      tabIndex={0}
                      aria-label={label}
                      sx={{
                        position: 'absolute',
                        insetBlock: 0,
                        left: `${left}%`,
                        width: `${width}%`,
                        minWidth:
                          interval.state === 'unregistered' || interval.state === 'offline'
                            ? 2
                            : 0,
                        bgcolor: stateColor(interval.state, viz),
                        outlineOffset: -2,
                        '&:focus-visible': { outline: `2px solid ${viz.focus}` },
                      }}
                    />
                  </MuiTooltip>
                )
              })}
            </Box>
          </Box>
        ))}
      </Stack>
      <Typography variant="caption" sx={{ display: 'block', mt: 1.25, color: 'text.secondary' }}>
        累计时长按状态采样间隔估算
      </Typography>
    </Box>
  )
}

export function SignalChart({ series, hours, onHoursChange }: Props) {
  const theme = useTheme()
  const mode = theme.palette.mode as Mode
  const viz = VIZ[mode]
  const [view, setView] = useState<'chart' | 'table'>('chart')
  const [mountedAt] = useState(() => Date.now())

  const samples = useMemo(() => buildSamples(series), [series])
  const newestSampleAt = useMemo(
    () => Math.max(0, ...[...samples.values()].flatMap((items) => items.map((item) => item.t))),
    [samples],
  )
  const rangeEnd = Math.max(mountedAt, newestSampleAt)
  const rangeStart = rangeEnd - hours * 60 * 60 * 1000
  const rows = useMemo(
    () => buildRows(series, samples, rangeStart, rangeEnd),
    [rangeEnd, rangeStart, samples, series],
  )

  const timelines = useMemo<TimelineRow[]>(
    () =>
      series.map((item) => {
        const itemSamples = samples.get(item.id) ?? []
        const intervals = buildRegistrationIntervals(
          itemSamples.map(({ sample }) => sample),
          rangeStart,
          rangeEnd,
        )
        return {
          series: item,
          intervals,
          summary: summarizeRegistration(intervals),
          currentState: intervals.at(-1)?.state ?? 'unknown',
        }
      }),
    [rangeEnd, rangeStart, samples, series],
  )

  const domain = useMemo<[number, number]>(() => {
    const values = rows.flatMap((row) =>
      series.map((item) => row[item.id]).filter((value): value is number => typeof value === 'number'),
    )
    if (!values.length) return [-113, -51]
    const min = Math.floor(Math.min(...values) / 10) * 10 - 5
    const max = Math.ceil(Math.max(...values) / 10) * 10 + 5
    return [Math.max(min, -120), Math.min(max, -40)]
  }, [rows, series])

  const latest = useMemo(() => {
    const output = new Map<string, SignalStatusSample | null>()
    for (const item of series) {
      output.set(item.id, samples.get(item.id)?.at(-1)?.sample ?? null)
    }
    return output
  }, [samples, series])

  const hasSignal = rows.some((row) => series.some((item) => typeof row[item.id] === 'number'))

  return (
    <Card>
      <CardHeader
        title={<Typography variant="h3">信号与注册状态</Typography>}
        subheader={
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            dBm 越接近 0 越强
          </Typography>
        }
      />
      <CardContent sx={{ pt: 0 }}>
        <Stack
          direction="row"
          spacing={1}
          useFlexGap
          sx={{ mb: 2, alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap' }}
        >
          <ToggleButtonGroup
            size="small"
            exclusive
            value={hours}
            onChange={(_, value) => value && onHoursChange(value)}
            aria-label="历史范围"
          >
            {RANGES.map((range) => (
              <ToggleButton key={range.value} value={range.value}>
                {range.label}
              </ToggleButton>
            ))}
          </ToggleButtonGroup>
          <ToggleButtonGroup
            size="small"
            exclusive
            value={view}
            onChange={(_, value) => value && setView(value)}
            aria-label="历史视图"
          >
            <ToggleButton value="chart">图表</ToggleButton>
            <ToggleButton value="table">数据</ToggleButton>
          </ToggleButtonGroup>
        </Stack>

        <Stack direction="row" spacing={3} useFlexGap sx={{ mb: 2, flexWrap: 'wrap', rowGap: 1 }}>
          {series.map((item) => {
            const current = latest.get(item.id)
            const state = current ? registrationState(current) : 'unknown'
            return (
              <Stack key={item.id} direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                <Box
                  aria-hidden
                  sx={{
                    width: 16,
                    height: 2,
                    borderRadius: 1,
                    bgcolor: seriesColor(mode, item.index),
                  }}
                />
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  {item.label}
                </Typography>
                <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                  {current?.dbm != null ? `${current.dbm} dBm` : '—'}
                </Typography>
                <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center' }}>
                  <StateKey state={state} viz={viz} />
                  <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                    {REGISTRATION_LABEL[state]}
                  </Typography>
                </Stack>
              </Stack>
            )
          })}
        </Stack>

        {rows.length === 0 ? (
          <Box sx={{ py: 6, textAlign: 'center' }}>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              还没有状态采样数据
            </Typography>
          </Box>
        ) : view === 'chart' ? (
          hasSignal ? (
            <Box sx={{ height: { xs: 260, sm: 300 } }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                  <CartesianGrid stroke={viz.gridline} strokeWidth={1} vertical={false} />
                  <XAxis
                    dataKey="t"
                    type="number"
                    scale="time"
                    domain={[rangeStart, rangeEnd]}
                    allowDataOverflow
                    tickFormatter={(t) => formatTime(t, hours)}
                    stroke={viz.axis}
                    tick={{ fill: viz.muted, fontSize: 12 }}
                    tickLine={false}
                    minTickGap={48}
                  />
                  <YAxis
                    domain={domain}
                    stroke={viz.axis}
                    tick={{ fill: viz.muted, fontSize: 12 }}
                    tickLine={false}
                    axisLine={false}
                    width={52}
                    tickFormatter={(value) => `${value}`}
                    label={{
                      value: 'dBm',
                      angle: -90,
                      position: 'insideLeft',
                      fill: viz.muted,
                      fontSize: 12,
                    }}
                  />
                  <ChartTooltip
                    cursor={{ stroke: viz.axis, strokeWidth: 1 }}
                    content={({ active, label }) => {
                      if (!active || label == null) return null
                      const time = Number(label)
                      return (
                        <Paper sx={{ px: 1.5, py: 1 }}>
                          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                            {formatTime(time, hours)}
                          </Typography>
                          {series.map((item) => {
                            const point = sampleAt(samples.get(item.id) ?? [], time)
                            const state = point ? registrationState(point) : 'unknown'
                            return (
                              <Stack
                                key={item.id}
                                direction="row"
                                spacing={1}
                                sx={{ mt: 0.25, alignItems: 'center' }}
                              >
                                <Box
                                  aria-hidden
                                  sx={{
                                    width: 10,
                                    height: 2,
                                    bgcolor: seriesColor(mode, item.index),
                                    borderRadius: 1,
                                  }}
                                />
                                <Typography variant="body2">{item.label}</Typography>
                                <Typography
                                  variant="body2"
                                  sx={{ ml: 'auto !important', fontVariantNumeric: 'tabular-nums' }}
                                >
                                  {point?.dbm != null ? `${point.dbm} dBm` : '—'}
                                </Typography>
                                <StateKey state={state} viz={viz} />
                                <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                                  {REGISTRATION_LABEL[state]}
                                </Typography>
                              </Stack>
                            )
                          })}
                        </Paper>
                      )
                    }}
                  />
                  {series.map((item) => (
                    <Line
                      key={item.id}
                      type="monotone"
                      dataKey={item.id}
                      name={item.label}
                      stroke={seriesColor(mode, item.index)}
                      strokeWidth={2}
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      dot={false}
                      activeDot={{ r: 4, strokeWidth: 2, stroke: viz.surface }}
                      connectNulls
                      isAnimationActive={false}
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </Box>
          ) : (
            <Box sx={{ py: 6, textAlign: 'center' }}>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                这段时间没有信号读数
              </Typography>
            </Box>
          )
        ) : (
          <TableContainer sx={{ maxHeight: 360 }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell>时间</TableCell>
                  {series.map((item) => (
                    <TableCell key={item.id} align="right">
                      {item.label}
                    </TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {[...rows].reverse().map((row) => (
                  <TableRow key={row.t}>
                    <TableCell>{formatTime(row.t, hours)}</TableCell>
                    {series.map((item) => {
                      const state = row[statusKey(item.id)] as RegistrationState | undefined
                      const value = row[item.id]
                      return (
                        <TableCell key={item.id} align="right">
                          <Typography variant="body2">
                            {typeof value === 'number' ? `${value} dBm` : '—'}
                          </Typography>
                          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                            {state ? REGISTRATION_LABEL[state] : '—'}
                          </Typography>
                        </TableCell>
                      )
                    })}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}

        {series.length > 0 && (
          <RegistrationTimeline
            rows={timelines}
            rangeStart={rangeStart}
            rangeEnd={rangeEnd}
            hours={hours}
            mode={mode}
            viz={viz}
          />
        )}
      </CardContent>
    </Card>
  )
}
