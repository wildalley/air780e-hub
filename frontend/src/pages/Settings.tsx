import { useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import VisibilityIcon from '@mui/icons-material/VisibilityOutlined'
import CopyIcon from '@mui/icons-material/ContentCopyOutlined'
import RotateIcon from '@mui/icons-material/PublishedWithChangesOutlined'
import { api, ApiError, type AgentTokenInfo } from '../api'
import { useToast } from '../toast'
import { PageHeader } from '../components/PageHeader'

export function SettingsPage({ onPasswordChanged }: { onPasswordChanged: () => void }) {
  const toast = useToast()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [tokenInfo, setTokenInfo] = useState<AgentTokenInfo | null>(null)
  const [rotateOpen, setRotateOpen] = useState(false)
  const [graceMinutes, setGraceMinutes] = useState(60)
  const [rotating, setRotating] = useState(false)
  const [purged, setPurged] = useState<Record<string, number> | null>(null)

  const changePassword = async () => {
    if (next !== confirm) {
      toast.show('两次输入的新密码不一致', 'error')
      return
    }
    try {
      await api.auth.changePassword(current, next)
      toast.show('密码已修改,所有会话已登出', 'success')
      setTimeout(onPasswordChanged, 1200)
    } catch (err) {
      toast.show(err instanceof ApiError ? err.message : '修改失败', 'error')
    }
  }

  const revealToken = async () => {
    try {
      setTokenInfo(await api.system.agentToken())
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : '读取 Token 失败', 'error')
    }
  }

  const rotateToken = async () => {
    setRotating(true)
    try {
      const replacement = await api.system.rotateAgentToken(graceMinutes)
      setTokenInfo((current) => ({
        token: replacement.token,
        rotatable: current?.rotatable ?? true,
        previous_valid_until: replacement.previous_valid_until,
      }))
      setRotateOpen(false)
      toast.show('Agent Token 已轮换', 'success')
    } catch (error) {
      toast.show(error instanceof ApiError ? error.message : '轮换失败', 'error')
    } finally {
      setRotating(false)
    }
  }

  return (
    <Stack spacing={3}>
      <PageHeader title="设置" subtitle="密码、Agent token 与数据清理" />

      <Card>
        <CardHeader title={<Typography variant="h3">修改管理员密码</Typography>} />
        <CardContent>
          <Stack spacing={2} sx={{ maxWidth: 420 }}>
            <TextField
              label="当前密码"
              type="password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              autoComplete="current-password"
            />
            <TextField
              label="新密码"
              type="password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              autoComplete="new-password"
              helperText="至少 8 位,需包含大写、小写、数字、符号中的至少两类"
            />
            <TextField
              label="确认新密码"
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
            />
            <Button
              variant="contained"
              onClick={changePassword}
              disabled={!current || !next || !confirm}
              sx={{ alignSelf: 'flex-start' }}
            >
              修改密码
            </Button>
            <Typography variant="caption" sx={{
              color: 'text.secondary'
            }}>
              修改后所有已登录的会话都会失效,包括当前这个。
            </Typography>
          </Stack>
        </CardContent>
      </Card>

      <Card>
        <CardHeader
          title={<Typography variant="h3">Agent Token</Typography>}
          subheader={
            <Typography variant="caption" sx={{
              color: 'text.secondary'
            }}>
              填在本地 <code>/etc/air780e-agent/config.toml</code> 的 <code>server.token</code>
            </Typography>
          }
        />
        <CardContent>
          {tokenInfo === null ? (
            <Button
              startIcon={<VisibilityIcon />}
              onClick={() => void revealToken()}
            >
              显示 Token
            </Button>
          ) : (
            <Stack spacing={2} sx={{
              alignItems: 'flex-start'
            }}>
              <Box
                component="code"
                sx={{
                  display: 'block',
                  width: '100%',
                  p: 2,
                  borderRadius: 2,
                  bgcolor: 'background.default',
                  border: 1,
                  borderColor: 'divider',
                  overflowWrap: 'anywhere',
                  fontSize: 13,
                }}
              >
                {tokenInfo.token}
              </Box>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                <Button
                  startIcon={<CopyIcon />}
                  onClick={() => {
                    void navigator.clipboard.writeText(tokenInfo.token).then(
                      () => toast.show('Token 已复制', 'success'),
                      () => toast.show('浏览器拒绝访问剪贴板', 'error'),
                    )
                  }}
                >
                  复制
                </Button>
                <Button
                  color="warning"
                  startIcon={<RotateIcon />}
                  disabled={!tokenInfo.rotatable}
                  onClick={() => setRotateOpen(true)}
                >
                  轮换 Token
                </Button>
              </Stack>
              {tokenInfo.previous_valid_until && (
                <Typography variant="caption" sx={{
                  color: 'warning.main'
                }}>
                  上一个 Token 可使用至 {new Date(tokenInfo.previous_valid_until).toLocaleString()}
                </Typography>
              )}
              {!tokenInfo.rotatable && (
                <Alert severity="info">
                  当前 Token 由 HUB_AGENT_TOKEN 环境变量管理，请在部署配置中轮换并重启服务。
                </Alert>
              )}
            </Stack>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader
          title={<Typography variant="h3">数据清理</Typography>}
          subheader={
            <Typography variant="caption" sx={{
              color: 'text.secondary'
            }}>
              按保留期删除过期短信、状态采样、日志、审计与已解决事件。保留期在容器环境变量里设置。
            </Typography>
          }
        />
        <CardContent>
          <Stack spacing={2} sx={{
            alignItems: 'flex-start'
          }}>
            <Button
              variant="outlined"
              onClick={async () => {
                setPurged(await api.system.purge())
                toast.show('清理完成', 'success')
              }}
            >
              立即清理
            </Button>
            {purged && (
              <Typography variant="body2" sx={{
                color: 'text.secondary'
              }}>
                删除:短信 {purged.messages ?? 0} 条,状态采样 {purged.status ?? 0} 条,日志{' '}
                {(purged.agent_logs ?? 0) + (purged.task_logs ?? 0) + (purged.notify_logs ?? 0)} 条,
                审计 {purged.audit_events ?? 0} 条,已解决事件 {purged.incidents ?? 0} 条
              </Typography>
            )}
          </Stack>
        </CardContent>
      </Card>

      <Alert severity="warning">
        这套系统存有你全部的短信验证码。建议保持默认保留期,不要把后台暴露给不必要的人,并定期备份
        <code> /data</code> 卷。
      </Alert>
      <Dialog open={rotateOpen} onClose={() => !rotating && setRotateOpen(false)}>
        <DialogTitle>轮换 Agent Token?</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: 2 }}>
            新 Token 会立即生效。宽限期内旧 Token 仍可重新连接，请在到期前更新全部 Agent 配置。
          </DialogContentText>
          <TextField
            autoFocus
            fullWidth
            type="number"
            label="旧 Token 宽限期（分钟）"
            value={graceMinutes}
            slotProps={{ htmlInput: { min: 0, max: 10080, step: 1 } }}
            onChange={(event) => {
              const value = Number(event.target.value)
              setGraceMinutes(Number.isFinite(value) ? Math.max(0, Math.min(10080, value)) : 0)
            }}
            helperText="0 表示旧 Token 立即失效，最大 7 天"
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRotateOpen(false)} disabled={rotating}>取消</Button>
          <Button
            variant="contained"
            color="warning"
            startIcon={<RotateIcon />}
            onClick={() => void rotateToken()}
            disabled={rotating}
          >
            确认轮换
          </Button>
        </DialogActions>
      </Dialog>
      {toast.element}
    </Stack>
  )
}
