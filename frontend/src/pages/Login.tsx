import { useState, type FormEvent } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  IconButton,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import LightIcon from '@mui/icons-material/LightModeOutlined'
import DarkIcon from '@mui/icons-material/DarkModeOutlined'
import { api, ApiError } from '../api'
import type { Mode } from '../tokens'

interface Props {
  needsSetup: boolean
  onAuthenticated: () => void | Promise<void>
  mode: Mode
  onToggleMode: () => void
}

export function LoginPage({ needsSetup, onAuthenticated, mode, onToggleMode }: Props) {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)

    if (needsSetup && password !== confirm) {
      setError('两次输入的密码不一致')
      return
    }

    setBusy(true)
    try {
      if (needsSetup) await api.auth.setup(password)
      else await api.auth.login(password)
      await onAuthenticated()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '登录失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Box
      sx={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        p: 2,
        bgcolor: 'background.default',
      }}
    >
      <IconButton
        onClick={onToggleMode}
        sx={{ position: 'fixed', top: 16, right: 16 }}
        aria-label="切换主题"
      >
        {mode === 'dark' ? <LightIcon /> : <DarkIcon />}
      </IconButton>

      <Card sx={{ width: '100%', maxWidth: 380 }}>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="h1" gutterBottom>
            air780e-hub
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
            {needsSetup ? '首次使用,请设置管理员密码' : '请输入管理员密码'}
          </Typography>

          <form onSubmit={submit}>
            <Stack spacing={2}>
              <TextField
                label="密码"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoFocus
                autoComplete={needsSetup ? 'new-password' : 'current-password'}
                fullWidth
                required
              />
              {needsSetup && (
                <>
                  <TextField
                    label="确认密码"
                    type="password"
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                    autoComplete="new-password"
                    fullWidth
                    required
                  />
                  <Typography variant="caption" color="text.secondary">
                    至少 8 位,且需包含大写、小写、数字、符号中的至少两类。
                  </Typography>
                </>
              )}
              {error && <Alert severity="error">{error}</Alert>}
              <Button type="submit" variant="contained" size="large" disabled={busy} fullWidth>
                {needsSetup ? '设置并进入' : '登录'}
              </Button>
            </Stack>
          </form>

          {needsSetup && (
            <Alert severity="info" sx={{ mt: 3 }}>
              这套系统会经手你全部的短信验证码,因此不提供免密模式。忘记密码可以 SSH 到服务器执行
              <code> hub-server auth reset-password</code>。
            </Alert>
          )}
        </CardContent>
      </Card>
    </Box>
  )
}
