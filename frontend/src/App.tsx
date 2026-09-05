import { useCallback, useEffect, useMemo, useState, lazy, Suspense } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { CssBaseline, GlobalStyles, ThemeProvider } from '@mui/material'
import useSWR, { SWRConfig, type SWRConfiguration } from 'swr'
import { api, errorText, onSessionLapse } from './api'
import { sessionState, type SessionEnd } from './session'
import { SWR_OPTIONS } from './swr'
import { buildTheme } from './theme'
import { VIZ, type Mode } from './tokens'
import { Layout } from './components/Layout'
import { ErrorState, Loading } from './components/common'
import { PageErrorBoundary } from './components/PageErrorBoundary'
import { LoginPage } from './pages/Login'
const DashboardPage = lazy(() => import('./pages/Dashboard').then((m) => ({ default: m.DashboardPage })))
const MessagesPage = lazy(() => import('./pages/Messages').then((m) => ({ default: m.MessagesPage })))
const DevicesPage = lazy(() => import('./pages/Devices').then((m) => ({ default: m.DevicesPage })))
const SimsPage = lazy(() => import('./pages/Sims').then((m) => ({ default: m.SimsPage })))
const TasksPage = lazy(() => import('./pages/Tasks').then((m) => ({ default: m.TasksPage })))
const ConsolePage = lazy(() => import('./pages/Console').then((m) => ({ default: m.ConsolePage })))
const NotifyPage = lazy(() => import('./pages/Notify').then((m) => ({ default: m.NotifyPage })))
const OperationsPage = lazy(() => import('./pages/Operations').then((m) => ({ default: m.OperationsPage })))
const LogsPage = lazy(() => import('./pages/Logs').then((m) => ({ default: m.LogsPage })))
const BackupPage = lazy(() => import('./pages/Backup').then((m) => ({ default: m.BackupPage })))
const SettingsPage = lazy(() => import('./pages/Settings').then((m) => ({ default: m.SettingsPage })))

const THEME_KEY = 'hub.theme'

function initialMode(): Mode {
  const stored = localStorage.getItem(THEME_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export default function App() {
  const [mode, setMode] = useState<Mode>(initialMode)
  // Why the business UI is down, when it is not simply "never logged in". A 401
  // from *any* request sets this, so the bounce to login is instant and needs no
  // round trip — the next status revalidation confirms it.
  const [ended, setEnded] = useState<SessionEnd | null>(null)
  // Set when the logout request itself failed. The local screen is cleared
  // either way; what is not known is whether the server tore the session down.
  const [logoutDoubt, setLogoutDoubt] = useState<string | null>(null)

  const {
    data: fetched,
    error: statusError,
    isLoading: statusLoading,
    mutate: mutateStatus,
  } = useSWR('/api/auth/status', () => api.auth.status(), SWR_OPTIONS)

  // This read deliberately stays outside the business cache below: bouncing that
  // cache on logout must not make the app forget how to ask who it is.
  const session = sessionState(fetched, statusError, ended)

  const theme = useMemo(() => buildTheme(mode), [mode])
  const viz = useMemo(() => VIZ[mode], [mode])

  useEffect(() => {
    document.documentElement.style.backgroundColor = theme.palette.background.default
    document.documentElement.style.colorScheme = mode
  }, [mode, theme.palette.background.default])

  const toggleMode = useCallback(() => {
    setMode((current) => {
      const next = current === 'dark' ? 'light' : 'dark'
      localStorage.setItem(THEME_KEY, next)
      return next
    })
  }, [])

  const refreshStatus = useCallback(async () => {
    setEnded(null)
    setLogoutDoubt(null)
    await mutateStatus()
  }, [mutateStatus])

  // A lapsed session anywhere in the app returns the whole UI to the login
  // screen rather than leaving pages showing stale data behind an error. The
  // request layer reports this itself, before the caller sees the rejection, so
  // it works for the writes and downloads that catch their own failures — those
  // never became unhandled rejections and so never used to arrive here.
  useEffect(() => onSessionLapse(() => setEnded('lapsed')), [])

  const logout = useCallback(async () => {
    // Local first, and unconditionally: whatever the server says, the messages
    // and channel secrets on this screen go away now.
    setEnded('signed-out')
    try {
      await api.auth.logout()
      setLogoutDoubt(null)
    } catch (err) {
      // Not "logged out" — the cookie may still be valid on the server. Say so
      // instead of implying a revocation that may not have happened.
      setLogoutDoubt(errorText(err, '退出请求失败'))
    }
    await mutateStatus()
  }, [mutateStatus])

  // Page-level reads share this, and a fresh cache is built for every login:
  // `provider` runs when this subtree mounts, so the previous session's
  // messages and diagnostics cannot be painted back onto the screen by a
  // re-login. In-memory drafts go the same way — they live in the pages, which
  // unmount with it.
  const swrConfig: SWRConfiguration = useMemo(
    () => ({ ...SWR_OPTIONS, provider: () => new Map() }),
    [],
  )

  let content
  if (session.phase === 'initializing') {
    // Never a blank plane: the status read is one request, but a slow one still
    // has to look like waiting rather than a broken page.
    content = <Loading />
  } else if (session.phase === 'unavailable') {
    content = (
      <ErrorState
        title="无法连接服务"
        message={session.notice ?? '状态接口没有响应。'}
        onRetry={() => void mutateStatus()}
        busy={statusLoading}
      />
    )
  } else if (session.phase === 'anonymous') {
    content = (
      <LoginPage
        needsSetup={session.needsSetup}
        onAuthenticated={refreshStatus}
        notice={session.notice}
        logoutDoubt={logoutDoubt}
        onRetryLogout={logout}
        mode={mode}
        onToggleMode={toggleMode}
      />
    )
  } else {
    content = (
      <SWRConfig value={swrConfig}>
      <BrowserRouter>
        <Layout mode={mode} onToggleMode={toggleMode} onLogout={logout}>
          <PageErrorBoundary>
            <Suspense fallback={<Loading />}>
              <Routes>
                <Route path="/" element={<DashboardPage />} />
                <Route path="/messages" element={<MessagesPage />} />
                <Route path="/devices" element={<DevicesPage />} />
                <Route path="/sims" element={<SimsPage />} />
                <Route path="/tasks" element={<TasksPage />} />
                <Route path="/console" element={<ConsolePage />} />
                <Route path="/notify" element={<NotifyPage />} />
                <Route path="/operations" element={<OperationsPage />} />
                <Route path="/logs" element={<LogsPage />} />
                <Route path="/backup" element={<BackupPage />} />
                <Route path="/settings" element={<SettingsPage onPasswordChanged={logout} />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </Suspense>
          </PageErrorBoundary>
        </Layout>
      </BrowserRouter>
      </SWRConfig>
    )
  }

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <GlobalStyles
        styles={{
          html: {
            minHeight: '100%',
            backgroundColor: theme.palette.background.default,
          },
          body: {
            minHeight: '100dvh',
            backgroundColor: theme.palette.background.default,
            WebkitFontSmoothing: 'antialiased',
            MozOsxFontSmoothing: 'grayscale',
            textRendering: 'optimizeLegibility',
          },
          '#root': {
            width: '100%',
            minHeight: '100dvh',
            display: 'flex',
            flexDirection: 'column',
            backgroundColor: theme.palette.background.default,
          },
          // Shared entrance used by dashboard tiles — `entranceStyle()` in common.
          '@keyframes hub-rise': {
            from: { opacity: 0, transform: 'translateY(14px)' },
            to: { opacity: 1, transform: 'none' },
          },
          '::selection': { backgroundColor: viz.selection, color: viz.selectionText },
          // Keyboard-only focus must be visible on every interactive element
          // (the a11y floor, not a polish pass).
          'a:focus-visible, button:focus-visible, [role="button"]:focus-visible, [role="tab"]:focus-visible, [role="switch"]:focus-visible, [role="checkbox"]:focus-visible, [role="radio"]:focus-visible':
            { outline: `2px solid ${viz.focus}`, outlineOffset: 2, borderRadius: 6 },
          // Subtle, quiet scrollbars that respect the palette.
          '*': { scrollbarWidth: 'thin', scrollbarColor: `${viz.axis} transparent` },
          '*::-webkit-scrollbar': { width: 10, height: 10 },
          '*::-webkit-scrollbar-thumb': {
            backgroundColor: viz.axis,
            borderRadius: 999,
            border: '2px solid transparent',
            backgroundClip: 'padding-box',
          },
          '*::-webkit-scrollbar-thumb:hover': { backgroundColor: viz.muted },
          // Motion collapses to instant for reduced-motion users (Apple §14).
          '@media (prefers-reduced-motion: reduce)': {
            '*, *::before, *::after': {
              animationDuration: '0.001ms !important',
              animationIterationCount: '1 !important',
              transitionDuration: '0.001ms !important',
              scrollBehavior: 'auto !important',
            },
          },
        }}
      />
      {content}
    </ThemeProvider>
  )
}
