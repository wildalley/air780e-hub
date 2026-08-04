import { useCallback, useEffect, useMemo, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { CssBaseline, ThemeProvider } from '@mui/material'
import { api, ApiError } from './api'
import { buildTheme } from './theme'
import type { Mode } from './tokens'
import { Layout } from './components/Layout'
import { Loading } from './components/common'
import { LoginPage } from './pages/Login'
import { DashboardPage } from './pages/Dashboard'
import { MessagesPage } from './pages/Messages'
import { DevicesPage } from './pages/Devices'
import { SimsPage } from './pages/Sims'
import { TasksPage } from './pages/Tasks'
import { ConsolePage } from './pages/Console'
import { NotifyPage } from './pages/Notify'
import { LogsPage } from './pages/Logs'
import { SettingsPage } from './pages/Settings'

const THEME_KEY = 'hub.theme'

function initialMode(): Mode {
  const stored = localStorage.getItem(THEME_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export default function App() {
  const [mode, setMode] = useState<Mode>(initialMode)
  const [status, setStatus] = useState<{ configured: boolean; authenticated: boolean } | null>(
    null,
  )

  const theme = useMemo(() => buildTheme(mode), [mode])

  const toggleMode = useCallback(() => {
    setMode((current) => {
      const next = current === 'dark' ? 'light' : 'dark'
      localStorage.setItem(THEME_KEY, next)
      return next
    })
  }, [])

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.auth.status())
    } catch {
      setStatus({ configured: true, authenticated: false })
    }
  }, [])

  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])

  // A lapsed session anywhere in the app returns the whole UI to the login
  // screen rather than leaving pages showing stale data behind an error.
  useEffect(() => {
    const onRejection = (event: PromiseRejectionEvent) => {
      if (event.reason instanceof ApiError && event.reason.isUnauthenticated) {
        setStatus((current) => (current ? { ...current, authenticated: false } : current))
      }
    }
    window.addEventListener('unhandledrejection', onRejection)
    return () => window.removeEventListener('unhandledrejection', onRejection)
  }, [])

  const logout = useCallback(async () => {
    await api.auth.logout().catch(() => undefined)
    await refreshStatus()
  }, [refreshStatus])

  let content
  if (status === null) {
    content = <Loading />
  } else if (!status.authenticated) {
    content = (
      <LoginPage
        needsSetup={!status.configured}
        onAuthenticated={refreshStatus}
        mode={mode}
        onToggleMode={toggleMode}
      />
    )
  } else {
    content = (
      <BrowserRouter>
        <Layout mode={mode} onToggleMode={toggleMode} onLogout={logout}>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/messages" element={<MessagesPage />} />
            <Route path="/devices" element={<DevicesPage />} />
            <Route path="/sims" element={<SimsPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/console" element={<ConsolePage />} />
            <Route path="/notify" element={<NotifyPage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/settings" element={<SettingsPage onPasswordChanged={logout} />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout>
      </BrowserRouter>
    )
  }

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      {content}
    </ThemeProvider>
  )
}
