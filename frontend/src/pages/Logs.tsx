import { useCallback, useEffect, useState } from 'react'
import {
  Card,
  CardContent,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tabs,
  Typography,
} from '@mui/material'
import { api, type AgentLog, type NotifyLog } from '../api'
import { Loading, formatTs } from '../components/common'
import { STATUS } from '../tokens'

const LEVEL_COLOR: Record<string, string> = {
  error: STATUS.critical,
  warning: STATUS.warning,
}

export function LogsPage() {
  const [tab, setTab] = useState(0)
  const [agentLogs, setAgentLogs] = useState<AgentLog[] | null>(null)
  const [notifyLogs, setNotifyLogs] = useState<NotifyLog[]>([])

  const load = useCallback(async () => {
    const [agent, notify] = await Promise.all([api.logs(), api.notifyLogs()])
    setAgentLogs(agent)
    setNotifyLogs(notify)
  }, [])

  useEffect(() => {
    void load()
    const timer = setInterval(() => void load(), 20_000)
    return () => clearInterval(timer)
  }, [load])

  if (agentLogs === null) return <Loading />

  return (
    <Stack spacing={3}>
      <Typography variant="h1">日志</Typography>

      <Card>
        <Tabs value={tab} onChange={(_, next) => setTab(next)} sx={{ px: 2 }}>
          <Tab label={`设备日志 (${agentLogs.length})`} />
          <Tab label={`推送日志 (${notifyLogs.length})`} />
        </Tabs>
        <CardContent sx={{ p: 0, '&:last-child': { pb: 0 } }}>
          {tab === 0 ? (
            agentLogs.length === 0 ? (
              <Typography variant="body2" color="text.secondary" sx={{ p: 4, textAlign: 'center' }}>
                还没有日志
              </Typography>
            ) : (
              <Table size="small">
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
            )
          ) : notifyLogs.length === 0 ? (
            <Typography variant="body2" color="text.secondary" sx={{ p: 4, textAlign: 'center' }}>
              还没有推送记录
            </Typography>
          ) : (
            <Table size="small">
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
          )}
        </CardContent>
      </Card>

      <Typography variant="caption" color="text.secondary">
        日志中不包含短信正文 —— agent 只记录发件人和长度。
      </Typography>
    </Stack>
  )
}
