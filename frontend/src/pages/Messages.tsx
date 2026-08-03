import { useCallback, useEffect, useState } from 'react'
import {
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  MenuItem,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TablePagination,
  TableRow,
  TextField,
  Typography,
} from '@mui/material'
import SendIcon from '@mui/icons-material/SendOutlined'
import CallReceivedIcon from '@mui/icons-material/CallReceived'
import CallMadeIcon from '@mui/icons-material/CallMade'
import { api, ApiError, type Device, type Message, type Sim } from '../api'
import { Loading, formatTs, useToast } from '../components/common'
import { STATUS } from '../tokens'

const PAGE_SIZE = 25

export function MessagesPage() {
  const toast = useToast()
  const [messages, setMessages] = useState<Message[] | null>(null)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [search, setSearch] = useState('')
  const [simId, setSimId] = useState<number | ''>('')
  const [direction, setDirection] = useState<'' | 'in' | 'out'>('')
  const [sims, setSims] = useState<Sim[]>([])
  const [devices, setDevices] = useState<Device[]>([])
  const [composeOpen, setComposeOpen] = useState(false)

  const load = useCallback(async () => {
    const data = await api.messages.list({
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
      sim_id: simId === '' ? undefined : simId,
      direction: direction === '' ? undefined : direction,
      search: search || undefined,
    })
    setMessages(data.items)
    setTotal(data.total)
  }, [page, simId, direction, search])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    void api.sims.list().then(setSims)
    void api.devices.list().then(setDevices)
  }, [])

  // One filter row above everything it scopes, never inside a card.
  return (
    <Stack spacing={3}>
      <Stack direction="row" alignItems="center" spacing={2}>
        <Typography variant="h1" sx={{ flexGrow: 1 }}>
          短信
        </Typography>
        <Button
          variant="contained"
          startIcon={<SendIcon />}
          onClick={() => setComposeOpen(true)}
          disabled={devices.length === 0}
        >
          发送
        </Button>
      </Stack>

      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        <TextField
          size="small"
          label="搜索内容或号码"
          value={search}
          onChange={(e) => {
            setPage(0)
            setSearch(e.target.value)
          }}
          sx={{ minWidth: 240 }}
        />
        <TextField
          size="small"
          select
          label="SIM 卡"
          value={simId}
          onChange={(e) => {
            setPage(0)
            setSimId(e.target.value === '' ? '' : Number(e.target.value))
          }}
          sx={{ minWidth: 160 }}
        >
          <MenuItem value="">全部</MenuItem>
          {sims.map((sim) => (
            <MenuItem key={sim.id} value={sim.id}>
              {sim.label || sim.iccid}
            </MenuItem>
          ))}
        </TextField>
        <TextField
          size="small"
          select
          label="方向"
          value={direction}
          onChange={(e) => {
            setPage(0)
            setDirection(e.target.value as '' | 'in' | 'out')
          }}
          sx={{ minWidth: 120 }}
        >
          <MenuItem value="">全部</MenuItem>
          <MenuItem value="in">接收</MenuItem>
          <MenuItem value="out">发送</MenuItem>
        </TextField>
      </Stack>

      <Card>
        <CardContent sx={{ p: 0, '&:last-child': { pb: 0 } }}>
          {messages === null ? (
            <Loading />
          ) : messages.length === 0 ? (
            <Typography variant="body2" color="text.secondary" sx={{ p: 4, textAlign: 'center' }}>
              没有匹配的短信
            </Typography>
          ) : (
            <>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell width={40} />
                    <TableCell>时间</TableCell>
                    <TableCell>卡</TableCell>
                    <TableCell>对方</TableCell>
                    <TableCell>内容</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {messages.map((message) => (
                    <TableRow key={message.id} hover>
                      <TableCell>
                        {message.direction === 'in' ? (
                          <CallReceivedIcon fontSize="small" color="disabled" />
                        ) : (
                          <CallMadeIcon fontSize="small" color="disabled" />
                        )}
                      </TableCell>
                      <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(message.ts)}</TableCell>
                      <TableCell sx={{ whiteSpace: 'nowrap' }}>
                        {message.sim_label || message.device}
                      </TableCell>
                      <TableCell sx={{ whiteSpace: 'nowrap' }}>{message.peer}</TableCell>
                      <TableCell>
                        <Box sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                          {message.body}
                        </Box>
                        {message.status === 'failed' && (
                          <Chip
                            size="small"
                            label={`发送失败:${message.error ?? '未知原因'}`}
                            sx={{ mt: 0.5, color: STATUS.critical }}
                            variant="outlined"
                          />
                        )}
                        {message.segments > 1 && (
                          <Typography variant="caption" color="text.secondary" display="block">
                            {message.segments} 段长短信
                          </Typography>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <TablePagination
                component="div"
                count={total}
                page={page}
                rowsPerPage={PAGE_SIZE}
                rowsPerPageOptions={[PAGE_SIZE]}
                onPageChange={(_, next) => setPage(next)}
                labelDisplayedRows={({ from, to, count }) => `${from}–${to} / ${count}`}
              />
            </>
          )}
        </CardContent>
      </Card>

      <ComposeDialog
        open={composeOpen}
        devices={devices}
        onClose={() => setComposeOpen(false)}
        onSent={async () => {
          setComposeOpen(false)
          toast.show('已发送', 'success')
          await load()
        }}
        onError={(message) => toast.show(message, 'error')}
      />
      {toast.element}
    </Stack>
  )
}

function ComposeDialog({
  open,
  devices,
  onClose,
  onSent,
  onError,
}: {
  open: boolean
  devices: Device[]
  onClose: () => void
  onSent: () => void | Promise<void>
  onError: (message: string) => void
}) {
  const [device, setDevice] = useState('')
  const [number, setNumber] = useState('')
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open && !device && devices.length) setDevice(devices[0].name)
  }, [open, device, devices])

  const send = async () => {
    setBusy(true)
    try {
      await api.messages.send(device, number, body)
      setNumber('')
      setBody('')
      await onSent()
    } catch (err) {
      // The agent's own error text (a +CMS code, "device offline") is far more
      // actionable than a generic failure.
      onError(err instanceof ApiError ? err.message : '发送失败')
    } finally {
      setBusy(false)
    }
  }

  const online = devices.find((d) => d.name === device)?.online

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>发送短信</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            select
            label="使用哪张卡"
            value={device}
            onChange={(e) => setDevice(e.target.value)}
            fullWidth
          >
            {devices.map((d) => (
              <MenuItem key={d.name} value={d.name} disabled={!d.online}>
                {d.sim_label || d.label || d.name}
                {d.online ? '' : '(离线)'}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="目标号码"
            value={number}
            onChange={(e) => setNumber(e.target.value)}
            placeholder="10086"
            fullWidth
          />
          <TextField
            label="内容"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            multiline
            minRows={3}
            fullWidth
            helperText={`${body.length} 字${body.length > 70 ? ' · 将拆成多段发送' : ''}`}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>取消</Button>
        <Button
          variant="contained"
          onClick={send}
          disabled={busy || !device || !number || !body || !online}
        >
          发送
        </Button>
      </DialogActions>
    </Dialog>
  )
}
