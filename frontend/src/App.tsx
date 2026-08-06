import { useCallback, useEffect, useMemo, useState, lazy, Suspense } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { CssBaseline, GlobalStyles, ThemeProvider } from '@mui/material'
import useSWR, { SWRConfig, type SWRConfiguration } from 'swr'
import { api, ApiError } from './api'
import { SWR_OPTIONS } from './swr'
import { buildTheme } from './theme'
import { VIZ, type Mode } from './tokens'
import { Layout } from './components/Layout'
import { Loading } from './components/common'
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
  // A 401 from *any* request means the cookie is gone. Tracked separately from
  // the fetched status so the bounce to login is instant and needs no round
  // trip — the next status revalidation confirms it.
  const [lapsed, setLapsed] = useState(false)

  const {
    data: fetched,
    error: statusError,
    mutate: mutateStatus,
  } = useSWR('/api/auth/status', () => api.auth.status(), SWR_OPTIONS)

  // The status endpoint is the one call that must never fail closed into a
  // blank screen: if it is unreachable, show the login form rather than an
  // indefinite spinner.
  const status = statusError ? { configured: true, authenticated: false } : fetched

  const theme = useMemo(() => buildTheme(mode), [mode])
  const viz = useMemo(() => VIZ[mode], [mode])

  const toggleMode = useCallback(() => {
    setMode((current) => {
      const next = current === 'dark' ? 'light' : 'dark'
      localStorage.setItem(THEME_KEY, next)
      return next
    })
  }, [])

  const refreshStatus = useCallback(async () => {
    setLapsed(false)
    await mutateStatus()
  }, [mutateStatus])

  // A lapsed session anywhere in the app returns the whole UI to the login
  // screen rather than leaving pages showing stale data behind an error.
  // Covers writes and downloads, which are plain promises; SWR reads are
  // caught by `onError` in the config below instead.
  useEffect(() => {
    const onRejection = (event: PromiseRejectionEvent) => {
      if (event.reason instanceof ApiError && event.reason.isUnauthenticated) setLapsed(true)
    }
    window.addEventListener('unhandledrejection', onRejection)
    return () => window.removeEventListener('unhandledrejection', onRejection)
  }, [])

  const logout = useCallback(async () => {
    await api.auth.logout().catch(() => undefined)
    await refreshStatus()
  }, [refreshStatus])

  // Page-level reads share this. `onError` is the read-side half of the
  // session-lapse handling above: SWR resolves failures into state rather
  // than rejecting, so an expired cookie would otherwise go unnoticed.
  const swrConfig: SWRConfiguration = useMemo(
    () => ({
      ...SWR_OPTIONS,
      onError: (error) => {
        if (error instanceof ApiError && error.isUnauthenticated) setLapsed(true)
      },
    }),
    [],
  )

  let content
  if (status === undefined) {
    content = null
  } else if (!status.authenticated || lapsed) {
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
      <SWRConfig value={swrConfig}>
      <BrowserRouter>
        <Layout mode={mode} onToggleMode={toggleMode} onLogout={logout}>
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
          'html, body, #root': { height: '100%', display: 'flex', flexDirection: 'column' },
          body: {
            WebkitFontSmoothing: 'antialiased',
            MozOsxFontSmoothing: 'grayscale',
            textRendering: 'optimizeLegibility',
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
