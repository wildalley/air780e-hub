import { useState } from 'react'
import {
  Card,
  CardContent,
  Chip,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tabs,
  Typography,
} from '@mui/material'
import useSWR from 'swr'
import { api } from '../api'
import { formatTs } from '../format'
import { usePager } from '../swr'
import { Loading, Pager } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { STATUS } from '../tokens'

const LEVEL_COLOR: Record<string, string> = {
  error: STATUS.critical,
  warning: STATUS.warning,
}

const CALL_OUTCOME: Record<string, string> = {
  answered: '已接听',
  missed: '未接',
  no_answer: '无应答',
  rejected: '已拒接',
  failed: '失败',
}

const REFRESH_MS = 20_000

export function LogsPage() {
  const [tab, setTab] = useState(0)

  // A pager per tab: paging one list must not move the other, and each keeps
  // its position while the other is on screen.
  const agentPager = usePager()
  const notifyPager = usePager()
  const callPager = usePager()

  // Two keys, not one composite fetch: the visible tab is the one that has to
  // be current, and a failure in either list leaves the other still rendering.
  // The paging params are in the key, so a page already visited is cached.
  const { data: agentPage } = useSWR(
    ['/api/logs', agentPager.query],
    () => api.logs(agentPager.query),
    { refreshInterval: REFRESH_MS, keepPreviousData: true },
  )
  const { data: notifyPage } = useSWR(
    ['/api/notify-logs', notifyPager.query],
    () => api.notifyLogs(notifyPager.query),
    { refreshInterval: REFRESH_MS, keepPreviousData: true },
  )
  const { data: callPage } = useSWR(
    ['/api/calls', callPager.query],
    () => api.calls.list({ limit: callPager.limit, offset: callPager.offset }),
    { refreshInterval: REFRESH_MS, keepPreviousData: true },
  )

  if (!agentPage) return <Loading />

  const agentLogs = agentPage.items
  const notifyLogs = notifyPage?.items ?? []
  const calls = callPage?.items ?? []

  return (
    <Stack spacing={3}>
      <PageHeader title="日志" subtitle="设备上报与推送记录,自动刷新" />

      <Card>
        <Tabs value={tab} onChange={(_, next) => setTab(next)} sx={{ px: 2 }}>
          {/* The total, not the page length — a page size of 50 would
              otherwise read as "50 logs" no matter how many there are. */}
          <Tab label={`设备日志 (${agentPage.total})`} />
          <Tab label={`推送日志 (${notifyPage?.total ?? 0})`} />
          <Tab label={`通话记录 (${callPage?.total ?? 0})`} />
        </Tabs>
        <CardContent sx={{ p: 0, '&:last-child': { pb: 0 } }}>
          {tab === 0 ? (
            agentLogs.length === 0 ? (
              <Typography
                variant="body2"
                sx={{
                  color: 'text.secondary',
                  p: 4,
                  textAlign: 'center'
                }}>
                还没有日志
              </Typography>
            ) : (
              <TableContainer>
                <Table size="small" sx={{ minWidth: 520 }}>
                  <TableHead>
                    <TableRow>
                      <TableCell>时间</TableCell>
                      <TableCell>设备</TableCell>
                      <TableCell>级别</TableCell>
                      <TableCell>内容</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {agentLogs.map((log) => (
                      <TableRow key={log.id}>
                        <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(log.ts)}</TableCell>
                        <TableCell>{log.device || log.agent_id}</TableCell>
                        <TableCell sx={{ color: LEVEL_COLOR[log.level] }}>{log.level}</TableCell>
                        <TableCell>{log.message}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                <Pager total={agentPage.total} pager={agentPager} />
              </TableContainer>
            )
          ) : tab === 1 ? (
            notifyLogs.length === 0 ? (
              <Typography
                variant="body2"
                sx={{
                  color: 'text.secondary',
                  p: 4,
                  textAlign: 'center'
                }}>
                还没有推送记录
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
                    {notifyLogs.map((log) => (
                      <TableRow key={log.id}>
                        <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(log.ts)}</TableCell>
                        <TableCell>{log.channel_name ?? log.channel_id}</TableCell>
                        <TableCell
                          sx={{ color: log.status === 'ok' ? STATUS.good : STATUS.critical }}
                        >
                          {log.status === 'ok' ? '成功' : '失败'}
                        </TableCell>
                        <TableCell>{log.attempts}</TableCell>
                        <TableCell>{log.detail}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                <Pager total={notifyPage?.total ?? 0} pager={notifyPager} />
              </TableContainer>
            )
          ) : calls.length === 0 ? (
            <Typography
              variant="body2"
              sx={{
                color: 'text.secondary',
                p: 4,
                textAlign: 'center'
              }}>
              还没有通话记录
            </Typography>
          ) : (
            <TableContainer>
              <Table size="small" sx={{ minWidth: 640 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>时间</TableCell>
                    <TableCell>方向</TableCell>
                    <TableCell>号码</TableCell>
                    <TableCell>卡</TableCell>
                    <TableCell>结果</TableCell>
                    <TableCell>响铃</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {calls.map((call) => (
                    <TableRow key={call.id}>
                      <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(call.ts)}</TableCell>
                      <TableCell>
                        <Chip
                          size="small"
                          label={call.direction === 'in' ? '来电' : '去电'}
                          color={call.direction === 'in' ? 'primary' : 'default'}
                          variant="outlined"
                          sx={{ minWidth: 56 }}
                        />
                      </TableCell>
                      <TableCell sx={{ fontVariantNumeric: 'tabular-nums' }}>
                        {call.peer}
                      </TableCell>
                      <TableCell>
                        {call.sim_label || call.sim_iccid?.slice(-6) || call.device}
                      </TableCell>
                      <TableCell>
                        <Chip
                          size="small"
                          label={CALL_OUTCOME[call.outcome] || call.outcome}
                          color={
                            call.outcome === 'answered' ? 'success' :
                            call.outcome === 'failed' ? 'error' :
                            'default'
                          }
                          variant="outlined"
                          sx={{ minWidth: 64 }}
                        />
                      </TableCell>
                      <TableCell sx={{ color: 'text.secondary' }}>
                        {call.ring_seconds > 0 ? `${Math.round(call.ring_seconds)}s` : '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <Pager total={callPage?.total ?? 0} pager={callPager} />
            </TableContainer>
          )}
        </CardContent>
      </Card>

      <Typography variant="caption" sx={{
        color: 'text.secondary'
      }}>
        日志中不包含短信正文 —— agent 只记录发件人和长度。
      </Typography>
    </Stack>
  )
}
