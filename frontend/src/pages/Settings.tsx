import { useEffect, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import VisibilityIcon from '@mui/icons-material/VisibilityOutlined'
import { api, ApiError } from '../api'
import { useToast } from '../components/common'
import { PageHeader } from '../components/PageHeader'

export function SettingsPage({ onPasswordChanged }: { onPasswordChanged: () => void }) {
  const toast = useToast()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [token, setToken] = useState<string | null>(null)
  const [purged, setPurged] = useState<Record<string, number> | null>(null)

  useEffect(() => {
    setPurged(null)
  }, [])

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
            <Typography variant="caption" color="text.secondary">
              修改后所有已登录的会话都会失效,包括当前这个。
            </Typography>
          </Stack>
        </CardContent>
      </Card>

      <Card>
        <CardHeader
          title={<Typography variant="h3">Agent Token</Typography>}
          subheader={
            <Typography variant="caption" color="text.secondary">
              填在本地 <code>/etc/air780e-agent/config.toml</code> 的 <code>server.token</code>
            </Typography>
          }
        />
        <CardContent>
          {token === null ? (
            <Button
              startIcon={<VisibilityIcon />}
              onClick={async () => setToken((await api.system.agentToken()).token)}
            >
              显示 token
            </Button>
          ) : (
            <Box
              component="code"
              sx={{
                display: 'block',
                p: 2,
                borderRadius: 2,
                bgcolor: 'background.default',
                border: 1,
                borderColor: 'divider',
                wordBreak: 'break-all',
                fontSize: 13,
              }}
            >
              {token}
            </Box>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader
          title={<Typography variant="h3">数据清理</Typography>}
          subheader={
            <Typography variant="caption" color="text.secondary">
              按保留期删除过期短信与状态采样。保留期在容器环境变量里设置。
            </Typography>
          }
        />
        <CardContent>
          <Stack spacing={2} alignItems="flex-start">
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
              <Typography variant="body2" color="text.secondary">
                删除:短信 {purged.messages ?? 0} 条,状态采样 {purged.status ?? 0} 条
              </Typography>
            )}
          </Stack>
        </CardContent>
      </Card>

      <Alert severity="warning">
        这套系统存有你全部的短信验证码。建议保持默认保留期,不要把后台暴露给不必要的人,并定期备份
        <code> /data</code> 卷。
      </Alert>
      {toast.element}
    </Stack>
  )
}
