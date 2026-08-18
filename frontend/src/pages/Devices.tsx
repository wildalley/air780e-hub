import { useState } from 'react'
import { Link as RouterLink } from 'react-router'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  CircularProgress,
  Divider,
  Drawer,
  IconButton,
  MenuItem,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
  TextField,
  Typography,
} from '@mui/material'
import FlightModeIcon from '@mui/icons-material/AirplanemodeActiveOutlined'
import UnknownRadioIcon from '@mui/icons-material/HelpOutlineOutlined'
import RefreshIcon from '@mui/icons-material/RefreshOutlined'
import RadioIcon from '@mui/icons-material/CellTowerOutlined'
import InfoIcon from '@mui/icons-material/InfoOutlined'
import TerminalIcon from '@mui/icons-material/TerminalOutlined'
import CloseIcon from '@mui/icons-material/Close'
import SearchIcon from '@mui/icons-material/SearchOutlined'
import AutoModeIcon from '@mui/icons-material/AutorenewOutlined'
import DiagnosticsIcon from '@mui/icons-material/TroubleshootOutlined'
import useSWR from 'swr'
import {
  api,
  ApiError,
  type Device,
  type NetworkDiagnostics,
  type OperatorNetwork,
} from '../api'
import { formatTs, relativeTs } from '../format'
import { useToast } from '../toast'
import { Loading, OnlineChip } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { StorageMeter } from '../components/StorageMeter'
import { SignalChart, type SignalSeries } from '../components/SignalChart'
import {
  formatDiagnostics,
  imsRegistrationStatus,
  networkRegistrationStatus,
  radioStatus,
} from '../deviceStatus'
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

  const reloadSelected = async () => {
    const updated = await load()
    setSelected((current) => {
      if (!current || !updated) return current
      return updated.find((device) => device.name === current.name) ?? current
    })
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
        subtitle="模块状态、信号、运营商与射频控制"
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

      <DeviceDrawer
        key={selected?.name ?? 'closed'}
        device={selected}
        onClose={() => setSelected(null)}
        onUpdated={reloadSelected}
        onError={(message) => toast.show(message, 'error')}
        onSuccess={(message) => toast.show(message, 'success')}
      />
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

function DeviceDrawer({
  device,
  onClose,
  onUpdated,
  onError,
  onSuccess,
}: {
  device: Device | null
  onClose: () => void
  onUpdated: () => Promise<unknown>
  onError: (message: string) => void
  onSuccess: (message: string) => void
}) {
  const [operators, setOperators] = useState<OperatorNetwork[]>([])
  const [operator, setOperator] = useState('')
  const [scanBusy, setScanBusy] = useState(false)
  const [selectBusy, setSelectBusy] = useState(false)
  const [diagnosticBusy, setDiagnosticBusy] = useState(false)
  const [diagnostics, setDiagnostics] = useState<NetworkDiagnostics | null>(null)

  const deviceName = device?.name

  const scan = async () => {
    if (!deviceName) return
    setScanBusy(true)
    try {
      const result = await api.devices.scanOperators(deviceName)
      setOperators(result.operators)
      setOperator(
        (current) =>
          current ||
          result.operators.find((item) => item.status === 2)?.numeric ||
          result.operators[0]?.numeric ||
          '',
      )
      onSuccess(result.operators.length ? `找到 ${result.operators.length} 个运营商` : '未找到可用运营商')
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '运营商扫描失败')
    } finally {
      setScanBusy(false)
    }
  }

  const select = async (numeric: string | null) => {
    if (!deviceName) return
    if (!window.confirm(numeric ? '切换运营商会暂时断开网络，继续？' : '恢复自动选网会重新注册网络，继续？')) {
      return
    }
    setSelectBusy(true)
    try {
      await api.devices.selectOperator(deviceName, numeric)
      await onUpdated()
      onSuccess(numeric ? '运营商选择命令已完成' : '已恢复自动选网')
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '运营商选择失败')
    } finally {
      setSelectBusy(false)
    }
  }

  const readDiagnostics = async () => {
    if (!deviceName) return
    setDiagnosticBusy(true)
    try {
      const result = await api.devices.networkDiagnostics(deviceName)
      setDiagnostics(result.diagnostics)
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '网络诊断失败')
    } finally {
      setDiagnosticBusy(false)
    }
  }

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
          <Divider sx={{ my: 2.5 }} />
          <Stack spacing={1.5}>
            <Typography variant="h3">运营商选择</Typography>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
              <Button
                variant="outlined"
                size="small"
                onClick={() => void scan()}
                disabled={!device.online || device.radio_enabled === 0 || scanBusy || selectBusy}
                startIcon={scanBusy ? <CircularProgress size={16} /> : <SearchIcon />}
              >
                {scanBusy ? '扫描中' : '扫描运营商'}
              </Button>
              <Button
                variant="text"
                size="small"
                onClick={() => void select(null)}
                disabled={!device.online || device.radio_enabled === 0 || scanBusy || selectBusy}
                startIcon={<AutoModeIcon />}
              >
                恢复自动
              </Button>
            </Stack>
            <TextField
              select
              size="small"
              label="扫描结果"
              value={operator}
              onChange={(event) => setOperator(event.target.value)}
              disabled={!operators.length || scanBusy || selectBusy}
            >
              {operators.map((item) => (
                <MenuItem key={item.numeric} value={item.numeric}>
                  {operatorLabel(item)}
                </MenuItem>
              ))}
            </TextField>
            <Button
              variant="contained"
              size="small"
              onClick={() => void select(operator || null)}
              disabled={!operator || !device.online || device.radio_enabled === 0 || scanBusy || selectBusy}
              startIcon={selectBusy ? <CircularProgress size={16} color="inherit" /> : <RadioIcon />}
            >
              {selectBusy ? '切换中' : '选择运营商'}
            </Button>
          </Stack>
          <Divider sx={{ my: 2.5 }} />
          <Stack spacing={1.5}>
            <Stack direction="row" sx={{ alignItems: 'center', justifyContent: 'space-between' }}>
              <Typography variant="h3">网络诊断</Typography>
              <Button
                variant="outlined"
                size="small"
                onClick={() => void readDiagnostics()}
                disabled={!device.online || diagnosticBusy}
                startIcon={diagnosticBusy ? <CircularProgress size={16} /> : <DiagnosticsIcon />}
              >
                读取
              </Button>
            </Stack>
            {diagnostics && (
              <Box
                component="pre"
                sx={{
                  m: 0,
                  p: 1.5,
                  maxHeight: 260,
                  overflow: 'auto',
                  whiteSpace: 'pre-wrap',
                  overflowWrap: 'anywhere',
                  bgcolor: 'action.hover',
                  borderRadius: 1,
                  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                  fontSize: '0.75rem',
                }}
              >
                {formatDiagnostics(diagnostics)}
              </Box>
            )}
          </Stack>
        </Box>
      )}
    </Drawer>
  )
}

function operatorLabel(operator: OperatorNetwork): string {
  const name = operator.long_name || operator.short_name || operator.numeric
  const technology = operator.access_technology === 7 ? 'LTE' : operator.access_technology === 0 ? 'GSM' : ''
  return `${name} (${operator.numeric}${technology ? ` · ${technology}` : ''})${operator.status === 2 ? ' · 当前' : ''}`
}
