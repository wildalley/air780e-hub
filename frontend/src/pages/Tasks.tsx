import { useCallback, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControlLabel,
  IconButton,
  MenuItem,
  Slider,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import EditIcon from '@mui/icons-material/EditOutlined'
import DeleteIcon from '@mui/icons-material/DeleteOutlined'
import useSWR from 'swr'
import { api, ApiError, type Device, type Task, type TaskInput } from '../api'
import { Loading, formatTs, useToast } from '../components/common'
import { PageHeader } from '../components/PageHeader'
import { STATUS } from '../tokens'

/** Every field the plan promised would be editable, with its default. */
const BLANK: TaskInput = {
  name: '',
  device: '',
  enabled: true,
  action: 'send_sms',
  target_number: '10086',
  content: '1',
  schedule_type: 'interval',
  schedule_expr: '25',
  jitter_seconds: 1800,
  random_suffix: true,
  retry_max: 3,
  notify_on_result: true,
}

function toInput(task: Task): TaskInput {
  return {
    name: task.name,
    device: task.device,
    sim_id: task.sim_id,
    enabled: Boolean(task.enabled),
    action: task.action,
    target_number: task.target_number,
    content: task.content,
    schedule_type: task.schedule_type,
    schedule_expr: task.schedule_expr,
    jitter_seconds: task.jitter_seconds,
    random_suffix: Boolean(task.random_suffix),
    retry_max: task.retry_max,
    notify_on_result: Boolean(task.notify_on_result),
  }
}

function describeSchedule(task: Task): string {
  if (task.schedule_type === 'interval') return `每 ${task.schedule_expr} 天`
  return `cron ${task.schedule_expr}`
}

export function TasksPage() {
  const toast = useToast()
  const [editing, setEditing] = useState<{ id: number | null; input: TaskInput } | null>(null)

  const { data: tasks, mutate: mutateTasks } = useSWR('/api/tasks', () => api.tasks.list())
  const { data: logs = [], mutate: mutateLogs } = useSWR('/api/task-logs', () => api.tasks.logs())
  // Shared cache key with the devices page and the composer — the dropdown of
  // modules costs nothing here if another view already fetched it.
  const { data: devices = [] } = useSWR('/api/devices', () => api.devices.list())

  // A write changes the task list and, once the agent reports back, its log.
  const load = useCallback(
    () => Promise.all([mutateTasks(), mutateLogs()]),
    [mutateTasks, mutateLogs],
  )

  const save = async () => {
    if (!editing) return
    try {
      if (editing.id === null) await api.tasks.create(editing.input)
      else await api.tasks.update(editing.id, editing.input)
      setEditing(null)
      toast.show('已保存并下发到 agent', 'success')
      await load()
    } catch (err) {
      toast.show(err instanceof ApiError ? err.message : '保存失败', 'error')
    }
  }

  const remove = async (task: Task) => {
    if (!window.confirm(`删除任务「${task.name || task.id}」?`)) return
    await api.tasks.remove(task.id)
    toast.show('已删除', 'success')
    await load()
  }

  if (!tasks) return <Loading />

  return (
    <Stack spacing={3}>
      <PageHeader
        title="保号任务"
        subtitle="定期动作由本地 agent 执行,不依赖服务器在线"
        actions={
          <Button
            variant="contained"
            startIcon={<AddIcon />}
            onClick={() =>
              setEditing({ id: null, input: { ...BLANK, device: devices[0]?.name ?? '' } })
            }
            disabled={devices.length === 0}
          >
            新建任务
          </Button>
        }
      />

      <Alert severity="info">
        任务在 <strong>本地 agent 上执行</strong>,不依赖服务器在线 —— 定时任务错过就补不回来,不能赌那条链路当时是通的。
        服务器只负责编辑规则并下发。
      </Alert>

      <Card>
        <CardContent sx={{ p: 0, '&:last-child': { pb: 0 } }}>
          {tasks.length === 0 ? (
            <Box sx={{ p: 4, textAlign: 'center' }}>
              <Typography variant="body2" sx={{
                color: 'text.secondary'
              }}>
                还没有保号任务。默认建议:每 25 天给运营商号码发一条短信。
              </Typography>
            </Box>
          ) : (
            <TableContainer>
              <Table size="small" sx={{ minWidth: 760 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>名称</TableCell>
                    <TableCell>卡 / 模块</TableCell>
                    <TableCell>动作</TableCell>
                    <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                      周期
                    </TableCell>
                    <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                      上次执行
                    </TableCell>
                    <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                      下次执行
                    </TableCell>
                    <TableCell>状态</TableCell>
                    <TableCell align="right" />
                  </TableRow>
                </TableHead>
              <TableBody>
                {tasks.map((task) => (
                  <TableRow key={task.id} hover>
                    <TableCell>{task.name || `任务 ${task.id}`}</TableCell>
                    <TableCell>{task.sim_label || task.device}</TableCell>
                    <TableCell>
                      {task.action === 'send_sms'
                        ? `发送到 ${task.target_number}:「${task.content}」`
                        : task.action === 'ping'
                          ? 'Ping 消耗流量'
                          : task.content}
                    </TableCell>
                    <TableCell sx={{ whiteSpace: 'nowrap', display: { xs: 'none', sm: 'table-cell' } }}>
                      {describeSchedule(task)}
                    </TableCell>
                    <TableCell sx={{ whiteSpace: 'nowrap', display: { xs: 'none', sm: 'table-cell' } }}>
                      {formatTs(task.last_run_at)}
                    </TableCell>
                    <TableCell sx={{ whiteSpace: 'nowrap', display: { xs: 'none', sm: 'table-cell' } }}>
                      {/* The agent owns the clock, jitter included — this is
                          what it reported back, not a guess made here. */}
                      {task.enabled ? formatTs(task.next_run_at) : '—'}
                    </TableCell>
                    <TableCell>
                      <Chip
                        size="small"
                        variant="outlined"
                        label={task.enabled ? '启用' : '停用'}
                        sx={{ color: task.enabled ? STATUS.good : 'text.secondary' }}
                      />
                    </TableCell>
                    <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                      <IconButton
                        size="small"
                        onClick={() => setEditing({ id: task.id, input: toInput(task) })}
                        aria-label="编辑"
                      >
                        <EditIcon fontSize="small" />
                      </IconButton>
                      <IconButton size="small" onClick={() => remove(task)} aria-label="删除">
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            </TableContainer>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <Typography variant="h3" gutterBottom>
            执行日志
          </Typography>
          {logs.length === 0 ? (
            <Typography variant="body2" sx={{
              color: 'text.secondary'
            }}>
              还没有执行记录
            </Typography>
          ) : (
            <TableContainer>
              <Table size="small" sx={{ minWidth: 560 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>时间</TableCell>
                    <TableCell>任务</TableCell>
                    <TableCell>结果</TableCell>
                    <TableCell>尝试</TableCell>
                    <TableCell>详情</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {logs.map((log) => (
                    <TableRow key={log.id}>
                      <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatTs(log.ts)}</TableCell>
                      <TableCell>{log.task_name || log.task_id}</TableCell>
                      <TableCell sx={{ color: log.status === 'ok' ? STATUS.good : STATUS.critical }}>
                        {log.status === 'ok' ? '成功' : log.status === 'skipped' ? '跳过' : '失败'}
                      </TableCell>
                      <TableCell>{log.attempts}</TableCell>
                      <TableCell>{log.error || log.detail}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
        </CardContent>
      </Card>

      {editing && (
        <TaskDialog
          value={editing.input}
          isNew={editing.id === null}
          devices={devices}
          onChange={(input) => setEditing({ ...editing, input })}
          onClose={() => setEditing(null)}
          onSave={save}
        />
      )}
      {toast.element}
    </Stack>
  )
}

function TaskDialog({
  value,
  isNew,
  devices,
  onChange,
  onClose,
  onSave,
}: {
  value: TaskInput
  isNew: boolean
  devices: Device[]
  onChange: (input: TaskInput) => void
  onClose: () => void
  onSave: () => void
}) {
  const set = <K extends keyof TaskInput>(key: K, next: TaskInput[K]) =>
    onChange({ ...value, [key]: next })

  const jitterMinutes = Math.round(value.jitter_seconds / 60)

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>{isNew ? '新建保号任务' : '编辑保号任务'}</DialogTitle>
      <DialogContent>
        <Stack spacing={2.5} sx={{ mt: 1 }}>
          <TextField
            label="任务名称"
            value={value.name}
            onChange={(e) => set('name', e.target.value)}
            placeholder="移动卡保号"
            fullWidth
          />

          <TextField
            select
            label="使用哪张卡"
            value={value.device}
            onChange={(e) => set('device', e.target.value)}
            fullWidth
          >
            {devices.map((device) => (
              <MenuItem key={device.name} value={device.name}>
                {device.sim_label || device.label || device.name}
              </MenuItem>
            ))}
          </TextField>

          <TextField
            select
            label="动作"
            value={value.action}
            onChange={(e) => set('action', e.target.value as TaskInput['action'])}
            fullWidth
          >
            <MenuItem value="send_sms">发送短信</MenuItem>
            <MenuItem value="ping">Ping(消耗流量)</MenuItem>
            <MenuItem value="raw_at">自定义 AT 指令</MenuItem>
          </TextField>

          {value.action === 'send_sms' && (
            <>
              <TextField
                label="目标号码"
                value={value.target_number}
                onChange={(e) => set('target_number', e.target.value)}
                fullWidth
                helperText="可以填运营商号码(10086/10010),也可以填你自己的另一个号码"
              />
              <TextField
                label="短信内容"
                value={value.content}
                onChange={(e) => set('content', e.target.value)}
                fullWidth
              />
            </>
          )}
          {value.action === 'raw_at' && (
            <TextField
              label="AT 指令"
              value={value.content}
              onChange={(e) => set('content', e.target.value)}
              fullWidth
              placeholder="AT+CIPPING=&quot;www.baidu.com&quot;"
            />
          )}

          <Divider />

          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: '5fr 7fr' }}>
            <TextField
              select
              label="调度方式"
              value={value.schedule_type}
              onChange={(e) =>
                onChange({
                  ...value,
                  schedule_type: e.target.value as TaskInput['schedule_type'],
                  // Swap in a sane default for the other mode rather than
                  // leaving "25" sitting in a cron field.
                  schedule_expr: e.target.value === 'interval' ? '25' : '0 3 * * 2',
                })
              }
              fullWidth
            >
              <MenuItem value="interval">间隔周期</MenuItem>
              <MenuItem value="cron">定点定时</MenuItem>
            </TextField>
            <TextField
              label={value.schedule_type === 'interval' ? '间隔天数' : 'cron 表达式'}
              value={value.schedule_expr}
              onChange={(e) => set('schedule_expr', e.target.value)}
              fullWidth
              helperText={
                value.schedule_type === 'interval'
                  ? '从上次执行时间起算'
                  : '分 时 日 月 周,本地时区'
              }
            />
          </Box>

          <Box>
            <Typography variant="body2" gutterBottom>
              随机延迟:±{jitterMinutes} 分钟
            </Typography>
            <Slider
              value={jitterMinutes}
              onChange={(_, next) => set('jitter_seconds', (next as number) * 60)}
              min={0}
              max={180}
              step={5}
              valueLabelDisplay="auto"
              valueLabelFormat={(v) => `${v} 分`}
            />
            <Typography variant="caption" sx={{
              color: 'text.secondary'
            }}>
              避开整点,让发送时间看起来不像机器
            </Typography>
          </Box>

          <TextField
            label="失败重试次数"
            type="number"
            value={value.retry_max}
            onChange={(e) => set('retry_max', Math.max(0, Number(e.target.value)))}
            fullWidth
            slotProps={{
              htmlInput: { min: 0, max: 10 }
            }}
          />

          <Tooltip title="在内容尾部附加几个随机字符。运营商对完全重复的短信可能拦截。">
            <FormControlLabel
              control={
                <Switch
                  checked={value.random_suffix}
                  onChange={(e) => set('random_suffix', e.target.checked)}
                />
              }
              label="内容附加随机字符(防拦截)"
            />
          </Tooltip>

          <FormControlLabel
            control={
              <Switch
                checked={value.notify_on_result}
                onChange={(e) => set('notify_on_result', e.target.checked)}
              />
            }
            label="执行结果推送通知"
          />

          <FormControlLabel
            control={
              <Switch checked={value.enabled} onChange={(e) => set('enabled', e.target.checked)} />
            }
            label="启用"
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>取消</Button>
        <Button variant="contained" onClick={onSave} disabled={!value.device}>
          保存
        </Button>
      </DialogActions>
    </Dialog>
  )
}
