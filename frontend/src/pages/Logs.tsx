import { useState } from 'react'
import {
  Card,
  CardContent,
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
import { Loading, formatTs } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { STATUS } from '../tokens'

const LEVEL_COLOR: Record<string, string> = {
  error: STATUS.critical,
  warning: STATUS.warning,
}

const REFRESH_MS = 20_000

export function LogsPage() {
  const [tab, setTab] = useState(0)

  // Two keys, not one composite fetch: the visible tab is the one that has to
  // be current, and a failure in either list leaves the other still rendering.
  const { data: agentLogs } = useSWR('/api/logs', () => api.logs(), {
    refreshInterval: REFRESH_MS,
  })
  const { data: notifyLogs = [] } = useSWR('/api/notify-logs', () => api.notifyLogs(), {
    refreshInterval: REFRESH_MS,
  })

  if (!agentLogs) return <Loading />

  return (
    <Stack spacing={3}>
      <PageHeader title="日志" subtitle="设备上报与推送记录,自动刷新" />

      <Card>
        <Tabs value={tab} onChange={(_, next) => setTab(next)} sx={{ px: 2 }}>
          <Tab label={`设备日志 (${agentLogs.length})`} />
          <Tab label={`推送日志 (${notifyLogs.length})`} />
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
              </TableContainer>
            )
          ) : notifyLogs.length === 0 ? (
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
