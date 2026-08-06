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
  Typography,
  useTheme,
} from '@mui/material'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { VIZ, seriesColor, type Mode } from '../tokens'
import type { StatusPoint } from '../api'

/**
 * Signal strength over time, one line per module.
 *
 * Design notes, in the order the decisions were made:
 *
 *  - Form: trend over time, and telling the modules apart is the point, so
 *    multi-line with a categorical palette.
 *  - ONE axis. dBm only. Storage lives in its own meter — putting two scales
 *    on one plot invents a correlation the data does not contain.
 *  - Colour follows the module, keyed by its position in the device list, so
 *    hiding a module never repaints the other one.
 *  - Values are reachable three ways: the current reading sits in the legend,
 *    the tooltip covers any point, and the table view is the WCAG-clean twin.
 *    The tooltip never *gates* a value.
 */

export interface SignalSeries {
  name: string
  label: string
  index: number
  points: StatusPoint[]
}

interface Props {
  series: SignalSeries[]
  hours: number
  onHoursChange: (hours: number) => void
}

interface Row {
  t: number
  [key: string]: number | null
}

const RANGES = [
  { value: 6, label: '6 小时' },
  { value: 24, label: '24 小时' },
  { value: 24 * 7, label: '7 天' },
]

function formatTime(t: number, hours: number): string {
  const d = new Date(t)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  if (hours <= 24) return `${hh}:${mm}`
  return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`
}

/** Merge per-device sample sets onto one shared time axis. */
function buildRows(series: SignalSeries[]): Row[] {
  const byTime = new Map<number, Row>()
  for (const s of series) {
    for (const point of s.points) {
      const t = new Date(point.ts).getTime()
      if (Number.isNaN(t)) continue
      let row = byTime.get(t)
      if (!row) {
        row = { t }
        byTime.set(t, row)
      }
      row[s.name] = point.dbm ?? null
    }
  }
  return [...byTime.values()].sort((a, b) => a.t - b.t)
}

export function SignalChart({ series, hours, onHoursChange }: Props) {
  const theme = useTheme()
  const mode = theme.palette.mode as Mode
  const viz = VIZ[mode]
  const [view, setView] = useState<'chart' | 'table'>('chart')

  const rows = useMemo(() => buildRows(series), [series])

  const domain = useMemo<[number, number]>(() => {
    const values = rows.flatMap((row) =>
      series.map((s) => row[s.name]).filter((v): v is number => typeof v === 'number'),
    )
    if (!values.length) return [-113, -51]
    // Round out to clean 10 dBm steps so the ticks read as round numbers.
    const min = Math.floor(Math.min(...values) / 10) * 10 - 5
    const max = Math.ceil(Math.max(...values) / 10) * 10 + 5
    return [Math.max(min, -120), Math.min(max, -40)]
  }, [rows, series])

  const latest = useMemo(() => {
    const out = new Map<string, number | null>()
    for (const s of series) {
      const last = [...s.points].reverse().find((p) => typeof p.dbm === 'number')
      out.set(s.name, last?.dbm ?? null)
    }
    return out
  }, [series])

  const empty = rows.length === 0

  return (
    <Card>
      <CardHeader
        title={<Typography variant="h3">信号强度</Typography>}
        subheader={
          <Typography variant="caption" sx={{
            color: 'text.secondary'
          }}>
            每个模块一条线,数值为 dBm(越接近 0 越强)
          </Typography>
        }
        action={
          <Stack direction="row" spacing={1} sx={{
            alignItems: 'center'
          }}>
            <ToggleButtonGroup
              size="small"
              exclusive
              value={hours}
              onChange={(_, value) => value && onHoursChange(value)}
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
            >
              <ToggleButton value="chart">图</ToggleButton>
              <ToggleButton value="table">表</ToggleButton>
            </ToggleButtonGroup>
          </Stack>
        }
      />
      <CardContent>
        {/* Legend is always present for two or more series, and carries the
            current reading so a value never depends on hovering. Text stays in
            text tokens; the coloured line-key beside it carries identity. */}
        <Stack direction="row" spacing={3} sx={{ mb: 2, flexWrap: 'wrap' }}>
          {series.map((s) => (
            <Stack key={s.name} direction="row" spacing={1} sx={{
              alignItems: 'center'
            }}>
              <Box
                aria-hidden
                sx={{
                  width: 16,
                  height: 2,
                  borderRadius: 1,
                  bgcolor: seriesColor(mode, s.index),
                }}
              />
              <Typography variant="body2" sx={{
                color: 'text.secondary'
              }}>
                {s.label}
              </Typography>
              <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                {latest.get(s.name) != null ? `${latest.get(s.name)} dBm` : '—'}
              </Typography>
            </Stack>
          ))}
        </Stack>

        {empty ? (
          <Box sx={{ py: 6, textAlign: 'center' }}>
            <Typography variant="body2" sx={{
              color: 'text.secondary'
            }}>
              还没有采样数据 —— agent 连上并上报后这里会有曲线
            </Typography>
          </Box>
        ) : view === 'chart' ? (
          // Height covers the plot *and* the x-axis band, so the card never
          // grows an inner scrollbar just to reach the tick labels.
          (<Box sx={{ height: 300 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                <CartesianGrid
                  stroke={viz.gridline}
                  strokeWidth={1}
                  vertical={false}
                />
                <XAxis
                  dataKey="t"
                  type="number"
                  scale="time"
                  domain={['dataMin', 'dataMax']}
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
                  tickFormatter={(v) => `${v}`}
                  label={{
                    value: 'dBm',
                    angle: -90,
                    position: 'insideLeft',
                    fill: viz.muted,
                    fontSize: 12,
                  }}
                />
                <Tooltip
                  cursor={{ stroke: viz.axis, strokeWidth: 1 }}
                  content={({ active, payload, label }) => {
                    if (!active || !payload?.length) return null
                    return (
                      <Paper sx={{ px: 1.5, py: 1 }}>
                        <Typography variant="caption" sx={{
                          color: 'text.secondary'
                        }}>
                          {formatTime(Number(label), hours)}
                        </Typography>
                        {payload.map((entry) => {
                          const s = series.find((item) => item.name === entry.dataKey)
                          return (
                            <Stack
                              key={String(entry.dataKey)}
                              direction="row"
                              spacing={1}
                              sx={{
                                alignItems: 'center'
                              }}
                            >
                              <Box
                                aria-hidden
                                sx={{
                                  width: 10,
                                  height: 2,
                                  bgcolor: entry.color,
                                  borderRadius: 1,
                                }}
                              />
                              <Typography variant="body2">
                                {s?.label ?? String(entry.dataKey)}
                              </Typography>
                              <Typography
                                variant="body2"
                                sx={{ fontVariantNumeric: 'tabular-nums' }}
                              >
                                {entry.value} dBm
                              </Typography>
                            </Stack>
                          )
                        })}
                      </Paper>
                    )
                  }}
                />
                {series.map((s) => (
                  <Line
                    key={s.name}
                    type="monotone"
                    dataKey={s.name}
                    name={s.label}
                    stroke={seriesColor(mode, s.index)}
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    dot={false}
                    // 8px marker (r=4) plus a 2px ring in the surface colour,
                    // so it stays legible where the two lines cross.
                    activeDot={{ r: 4, strokeWidth: 2, stroke: viz.surface }}
                    connectNulls
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </Box>)
        ) : (
          <TableContainer sx={{ maxHeight: 300 }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell>时间</TableCell>
                  {series.map((s) => (
                    <TableCell key={s.name} align="right">
                      {s.label} (dBm)
                    </TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {[...rows].reverse().map((row) => (
                  <TableRow key={row.t}>
                    <TableCell>{formatTime(row.t, hours)}</TableCell>
                    {series.map((s) => (
                      <TableCell key={s.name} align="right">
                        {row[s.name] ?? '—'}
                      </TableCell>
                    ))}
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
