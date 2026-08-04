import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Grid,
  MenuItem,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import RefreshIcon from '@mui/icons-material/RefreshOutlined'
import TerminalIcon from '@mui/icons-material/TerminalOutlined'
import { api, ApiError, type Device } from '../api'
import { Loading, OnlineChip, formatTs, useToast } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { StorageMeter } from '../components/StorageMeter'

export function DevicesPage() {
  const toast = useToast()
  const [devices, setDevices] = useState<Device[] | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(async () => setDevices(await api.devices.list()), [])

  useEffect(() => {
    void load()
  }, [load])

  const refresh = async (device: Device) => {
    setBusy(device.name)
    try {
      await api.devices.refresh(device.name)
      await load()
      toast.show('已刷新', 'success')
    } catch (err) {
      toast.show(err instanceof ApiError ? err.message : '刷新失败', 'error')
    } finally {
      setBusy(null)
    }
  }

  if (devices === null) return <Loading />

  return (
    <Stack spacing={3}>
      <PageHeader title="设备" subtitle="各模块的实时状态与工程参数" />

      {devices.length === 0 && (
        <Alert severity="info">
          还没有设备上报。检查本地 agent 是否已连上:<code>journalctl -u air780e-agent -f</code>
        </Alert>
      )}

      <Grid container spacing={2}>
        {devices.map((device) => (
          <Grid item xs={12} md={6} key={device.id}>
            <Card>
              <CardHeader
                title={
                  <Typography variant="h3">
                    {device.sim_label || device.label || device.name}
                  </Typography>
                }
                subheader={
                  <Typography variant="caption" color="text.secondary">
                    {device.port} · agent {device.agent_id}
                  </Typography>
                }
                action={<OnlineChip online={Boolean(device.online)} />}
              />
              <CardContent>
                <Stack spacing={2}>
                  <Grid container spacing={1.5}>
                    {[
                      ['型号', device.model || '—'],
                      ['IMEI', device.imei || '—'],
                      ['ICCID', device.iccid || '无卡'],
                      ['运营商', device.operator || '未注册'],
                      ['信号', device.dbm != null ? `${device.dbm} dBm (${device.bars}/5)` : '—'],
                      ['RSRP / RSRQ', device.rsrp != null ? `${device.rsrp} / ${device.rsrq}` : '—'],
                      ['注册状态', device.registered ? '已注册' : '未注册'],
                      ['最后上报', formatTs(device.last_seen_at)],
                    ].map(([label, value]) => (
                      <Grid item xs={6} key={label}>
                        <Typography variant="caption" color="text.secondary" display="block">
                          {label}
                        </Typography>
                        <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
                          {value}
                        </Typography>
                      </Grid>
                    ))}
                  </Grid>

                  <StorageMeter used={device.storage_used} capacity={device.storage_cap} />

                  <Button
                    size="small"
                    startIcon={<RefreshIcon />}
                    onClick={() => refresh(device)}
                    disabled={!device.online || busy === device.name}
                    sx={{ alignSelf: 'flex-start' }}
                  >
                    立即刷新
                  </Button>
                </Stack>
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      <AtConsole devices={devices} onError={(m) => toast.show(m, 'error')} />
      {toast.element}
    </Stack>
  )
}

/**
 * Raw AT console.
 *
 * Sharp edge on purpose: this reaches the modem directly. It exists because
 * bring-up and field debugging need it, and it is behind the admin session
 * like everything else.
 */
function AtConsole({
  devices,
  onError,
}: {
  devices: Device[]
  onError: (message: string) => void
}) {
  const [device, setDevice] = useState(devices[0]?.name ?? '')
  const [command, setCommand] = useState('AT+CSQ')
  const [output, setOutput] = useState<string[]>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!device && devices.length) setDevice(devices[0].name)
  }, [device, devices])

  const run = async () => {
    setBusy(true)
    try {
      const result = await api.at(device, command)
      setOutput((previous) => [
        ...previous,
        `> ${command}`,
        ...(result.lines.length ? result.lines : ['OK']),
        '',
      ])
    } catch (err) {
      const message = err instanceof ApiError ? err.message : '执行失败'
      setOutput((previous) => [...previous, `> ${command}`, `!! ${message}`, ''])
      onError(message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader
        avatar={<TerminalIcon />}
        title={<Typography variant="h3">AT 调试台</Typography>}
        subheader={
          <Typography variant="caption" color="text.secondary">
            直接下发 AT 指令到模块。参考 docs.openluat.com/air780e/at
          </Typography>
        }
      />
      <CardContent>
        <Stack spacing={2}>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
            <TextField
              select
              size="small"
              label="设备"
              value={device}
              onChange={(e) => setDevice(e.target.value)}
              sx={{ minWidth: 180 }}
            >
              {devices.map((d) => (
                <MenuItem key={d.name} value={d.name} disabled={!d.online}>
                  {d.sim_label || d.label || d.name}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              size="small"
              label="指令"
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !busy) void run()
              }}
              sx={{ flexGrow: 1 }}
            />
            <Button variant="outlined" onClick={run} disabled={busy || !device || !command}>
              执行
            </Button>
          </Stack>

          {output.length > 0 && (
            <Box
              component="pre"
              sx={{
                m: 0,
                p: 2,
                borderRadius: 2,
                bgcolor: 'background.default',
                border: 1,
                borderColor: 'divider',
                fontSize: 13,
                maxHeight: 260,
                overflow: 'auto',
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              }}
            >
              {output.join('\n')}
            </Box>
          )}
        </Stack>
      </CardContent>
    </Card>
  )
}
