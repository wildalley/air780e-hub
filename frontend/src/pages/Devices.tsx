import { useState } from 'react'
import { Link as RouterLink } from 'react-router'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Divider,
  Drawer,
  IconButton,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material'
import FlightModeIcon from '@mui/icons-material/AirplanemodeActiveOutlined'
import UnknownRadioIcon from '@mui/icons-material/HelpOutlineOutlined'
import RefreshIcon from '@mui/icons-material/RefreshOutlined'
import RadioIcon from '@mui/icons-material/CellTowerOutlined'
import InfoIcon from '@mui/icons-material/InfoOutlined'
import TerminalIcon from '@mui/icons-material/TerminalOutlined'
import CloseIcon from '@mui/icons-material/Close'
import useSWR from 'swr'
import { api, ApiError, type Device } from '../api'
import { formatTs, relativeTs } from '../format'
import { useToast } from '../toast'
import { Loading, OnlineChip } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { StorageMeter } from '../components/StorageMeter'
import { SignalChart, type SignalSeries } from '../components/SignalChart'
import { imsRegistrationStatus, networkRegistrationStatus, radioStatus } from '../deviceStatus'
import { LIVE_MS } from '../swr'

function deviceLabel(device: Device): string {
  return device.sim_label || device.label || device.name
}

export function DevicesPage() {
  const toast = useToast()
  const [busy, setBusy] = useState<string | null>(null)
  const [selected, setSelected] = useState<Device | null>(null)
  const [historyHours, setHistoryHours] = useState(24)

  const { data: devices, mutate: load } = useSWR('/api/devices', () => api.devices.list())
  const { data: history = {} } = useSWR(
    ['/api/devices/history', historyHours],
    () => api.devices.histories(historyHours),
    { refreshInterval: LIVE_MS, keepPreviousData: true },
  )

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

  const setRadio = async (device: Device, enabled: boolean) => {
    if (
      !enabled &&
      !window.confirm('关闭射频后，这张卡将无法收发短信，保号任务会暂停。继续？')
    ) {
      return
    }
    setBusy(device.name)
    try {
      await api.devices.setRadio(device.name, enabled)
      await load()
      toast.show(enabled ? '射频已开启' : '已进入飞行模式', 'success')
    } catch (err) {
      toast.show(err instanceof ApiError ? err.message : '射频切换失败', 'error')
    } finally {
      setBusy(null)
    }
  }

  if (!devices) return <Loading />

  const signalSeries: SignalSeries[] = devices.map((device, index) => ({
    name: device.name,
    label: deviceLabel(device),
    index,
    points: history[device.name] ?? [],
    current: device.last_seen_at
      ? {
          ts: device.last_seen_at,
          online: device.online,
          registered: device.registered,
          dbm: device.dbm,
        }
      : undefined,
  }))

  return (
    <Stack spacing={3}>
      <PageHeader
        title="设备"
        subtitle="模块状态、信号与射频控制"
        actions={
          <Button component={RouterLink} to="/console" startIcon={<TerminalIcon />}>
            AT 调试
          </Button>
        }
      />

      {devices.length === 0 && (
        <Alert severity="info">
          还没有设备上报。检查本地 agent 是否已连上:<code>journalctl -u air780e-agent -f</code>
        </Alert>
      )}

      {devices.length > 0 && (
        <>
          <Card sx={{ display: { xs: 'none', md: 'block' } }}>
            <TableContainer>
              <Table size="small" sx={{ minWidth: 960 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>卡 / 模块</TableCell>
                    <TableCell>连接</TableCell>
                    <TableCell>信号</TableCell>
                    <TableCell sx={{ width: 190 }}>存储</TableCell>
                    <TableCell sx={{ width: 220 }}>移动网络射频</TableCell>
                    <TableCell>最后上报</TableCell>
                    <TableCell align="right" />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {devices.map((device) => (
                    <TableRow key={device.id} hover>
                      <TableCell>
                        <Typography variant="body2" sx={{ fontWeight: 650 }}>
                          {deviceLabel(device)}
                        </Typography>
                        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                          {device.name} · {device.operator || '未知运营商'}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Stack spacing={0.5} sx={{ alignItems: 'flex-start' }}>
                          <OnlineChip online={Boolean(device.online)} />
                          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                            {networkRegistrationStatus(device)}
                          </Typography>
                          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                            {imsRegistrationStatus(device)}
                          </Typography>
                        </Stack>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" sx={{ whiteSpace: 'nowrap' }}>
                          {device.dbm != null ? `${device.dbm} dBm` : '—'}
                        </Typography>
                        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                          {device.dbm != null ? `${device.bars}/5 格` : '无数据'}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <StorageMeter
                          used={device.storage_used}
                          capacity={device.storage_cap}
                          label="短信存储"
                        />
                      </TableCell>
                      <TableCell>
                        <RadioControl
                          device={device}
                          busy={busy === device.name}
                          onChange={(enabled) => void setRadio(device, enabled)}
                          compact
                        />
                      </TableCell>
                      <TableCell sx={{ whiteSpace: 'nowrap' }}>
                        <Typography variant="body2">{relativeTs(device.last_seen_at)}</Typography>
                        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                          {formatTs(device.last_seen_at)}
                        </Typography>
                      </TableCell>
                      <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                        <Tooltip title="刷新状态">
                          <span>
                            <IconButton
                              size="small"
                              onClick={() => void refresh(device)}
                              disabled={!device.online || busy === device.name}
                              aria-label={`刷新 ${deviceLabel(device)}`}
                            >
                              <RefreshIcon fontSize="small" />
                            </IconButton>
                          </span>
                        </Tooltip>
                        <Tooltip title="设备详情">
                          <IconButton
                            size="small"
                            onClick={() => setSelected(device)}
                            aria-label={`查看 ${deviceLabel(device)} 详情`}
                          >
                            <InfoIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </Card>

          <Stack spacing={2} sx={{ display: { xs: 'flex', md: 'none' } }}>
            {devices.map((device) => (
              <Card key={device.id}>
                <CardHeader
                  title={<Typography variant="h3">{deviceLabel(device)}</Typography>}
                  subheader={
                    <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                      {device.name} · {device.operator || '未知运营商'}
                    </Typography>
                  }
                  action={<OnlineChip online={Boolean(device.online)} />}
                />
                <CardContent sx={{ pt: 0 }}>
                  <Stack spacing={2}>
                    <Box
                      sx={{
                        display: 'grid',
                        gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
                        gap: 1.5,
                      }}
                    >
                      <SummaryValue label="移动网络" value={networkRegistrationStatus(device)} />
                      <SummaryValue label="IMS" value={imsRegistrationStatus(device)} />
                      <SummaryValue
                        label="信号"
                        value={device.dbm != null ? `${device.dbm} dBm (${device.bars}/5)` : '—'}
                      />
                      <SummaryValue label="最后上报" value={relativeTs(device.last_seen_at)} />
                      <SummaryValue label="端口" value={device.port || '—'} />
                    </Box>
                    <StorageMeter used={device.storage_used} capacity={device.storage_cap} />
                    <RadioControl
                      device={device}
                      busy={busy === device.name}
                      onChange={(enabled) => void setRadio(device, enabled)}
                    />
                    <Stack direction="row" spacing={1} sx={{ justifyContent: 'space-between' }}>
                      <Button
                        size="small"
                        startIcon={<RefreshIcon />}
                        onClick={() => void refresh(device)}
                        disabled={!device.online || busy === device.name}
                      >
                        刷新
                      </Button>
                      <Button size="small" startIcon={<InfoIcon />} onClick={() => setSelected(device)}>
                        详情
                      </Button>
                    </Stack>
                  </Stack>
                </CardContent>
              </Card>
            ))}
          </Stack>

          <SignalChart
            series={signalSeries}
            hours={historyHours}
            onHoursChange={setHistoryHours}
          />
        </>
      )}

      <DeviceDrawer device={selected} onClose={() => setSelected(null)} />
      {toast.element}
    </Stack>
  )
}

function RadioControl({
  device,
  busy,
  onChange,
  compact = false,
}: {
  device: Device
  busy: boolean
  onChange: (enabled: boolean) => void
  compact?: boolean
}) {
  const icon =
    device.radio_enabled == null ? (
      <UnknownRadioIcon color="action" fontSize="small" />
    ) : device.radio_enabled ? (
      <RadioIcon color="action" fontSize="small" />
    ) : (
      <FlightModeIcon color="action" fontSize="small" />
    )

  return (
    <Stack
      direction="row"
      spacing={1}
      sx={{ alignItems: 'center', justifyContent: 'space-between', minHeight: compact ? 36 : 42 }}
    >
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center', minWidth: 0 }}>
        {icon}
        <Box sx={{ minWidth: 0 }}>
          {!compact && (
            <Typography variant="body2" sx={{ fontWeight: 600 }}>
              移动网络射频
            </Typography>
          )}
          <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
            {busy ? '切换中' : radioStatus(device)}
          </Typography>
        </Box>
      </Stack>
      <Switch
        size={compact ? 'small' : 'medium'}
        checked={Boolean(device.radio_enabled)}
        onChange={(_, enabled) => onChange(enabled)}
        disabled={!device.online || device.radio_enabled == null || busy}
        slotProps={{ input: { 'aria-label': `${device.name} 移动网络射频` } }}
      />
    </Stack>
  )
}

function SummaryValue({ label, value }: { label: string; value: string }) {
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>
        {value}
      </Typography>
    </Box>
  )
}

function DeviceDrawer({ device, onClose }: { device: Device | null; onClose: () => void }) {
  const fields = device
    ? [
        ['模块名称', device.name],
        ['Agent', device.agent_id],
        ['设备端口', device.port || '—'],
        ['AT 型号', device.model || '—'],
        ['硬件型号', device.hardware_model || '—'],
        ['固件版本', device.firmware || '—'],
        ['移动网络', networkRegistrationStatus(device)],
        ['IMS 注册', imsRegistrationStatus(device)],
        ['IMEI', device.imei || '—'],
        ['ICCID', device.iccid || '无卡'],
        ['电话号码', device.phone_number || '—'],
        ['运营商', device.operator || '—'],
        ['RSRP / RSRQ', device.rsrp != null ? `${device.rsrp} / ${device.rsrq ?? '—'}` : '—'],
        ['最后上报', formatTs(device.last_seen_at)],
      ]
    : []

  return (
    <Drawer
      anchor="right"
      open={device !== null}
      onClose={onClose}
      slotProps={{ paper: { sx: { width: { xs: '100%', sm: 420 }, maxWidth: '100%' } } }}
    >
      {device && (
        <Box sx={{ p: 3 }}>
          <Stack direction="row" sx={{ alignItems: 'center', justifyContent: 'space-between' }}>
            <Box sx={{ minWidth: 0 }}>
              <Typography variant="h2">{deviceLabel(device)}</Typography>
              <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.5 }}>
                {device.online ? '模块在线' : '模块离线'} · {radioStatus(device)}
              </Typography>
            </Box>
            <IconButton onClick={onClose} aria-label="关闭设备详情">
              <CloseIcon />
            </IconButton>
          </Stack>
          <Divider sx={{ my: 2.5 }} />
          <Box
            component="dl"
            sx={{
              m: 0,
              display: 'grid',
              gridTemplateColumns: '110px minmax(0, 1fr)',
              rowGap: 2,
              columnGap: 2,
            }}
          >
            {fields.map(([label, value]) => (
              <Box key={label} sx={{ display: 'contents' }}>
                <Typography component="dt" variant="body2" sx={{ color: 'text.secondary' }}>
                  {label}
                </Typography>
                <Typography component="dd" variant="body2" sx={{ m: 0, overflowWrap: 'anywhere' }}>
                  {value}
                </Typography>
              </Box>
            ))}
          </Box>
        </Box>
      )}
    </Drawer>
  )
}
