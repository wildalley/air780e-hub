import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Snackbar,
  TableCell,
  TableRow,
  useMediaQuery,
} from '@mui/material'
import type { CSSObject } from '@mui/material/styles'
import CheckIcon from '@mui/icons-material/CheckCircleOutlined'
import OfflineIcon from '@mui/icons-material/CloudOffOutlined'
import { useCallback, useState } from 'react'
import { STATUS } from '../tokens'

/**
 * Small shared pieces.
 *
 * Status is never colour alone — every chip carries an icon and a word, which
 * is what keeps it readable under CVD and in forced-colors mode.
 */

export function useReducedMotion() {
  return useMediaQuery('(prefers-reduced-motion: reduce)')
}

/**
 * Mount entrance: a short rise + fade, gated behind
 * `prefers-reduced-motion: no-preference`. Delays let a grid stagger in.
 * Sprinkled on a handful of dashboard surfaces, never on data that refreshes.
 */
export function entranceStyle(delay = 0): CSSObject {
  return {
    '@media (prefers-reduced-motion: no-preference)': {
      animation: 'hub-rise 540ms cubic-bezier(0.16, 1, 0.3, 1) both',
      animationDelay: `${delay}ms`,
    },
  }
}

export function OnlineChip({ online }: { online: boolean }) {
  return (
    <Chip
      size="small"
      icon={online ? <CheckIcon /> : <OfflineIcon />}
      label={online ? '在线' : '离线'}
      sx={{
        color: online ? STATUS.good : 'text.secondary',
        borderColor: online ? STATUS.good : undefined,
        '& .MuiChip-icon': { color: 'inherit' },
      }}
      variant="outlined"
    />
  )
}

/**
 * Empty state for a table that scrolls horizontally.
 *
 * `align="center"` on a `colSpan` cell centres against the *table's* width,
 * not the viewport's. Once `minWidth` exceeds the screen the container
 * scrolls, and the centred label is parked somewhere off to the right — on a
 * phone you see a few clipped characters at the edge, or nothing at all.
 *
 * So the label is left-aligned and pinned with `position: sticky`, which keeps
 * it against the left edge of the scrollport no matter where the user has
 * scrolled to. Centring is what breaks; visibility is what matters.
 */
export function EmptyRow({ colSpan, children, py = 4 }: {
  colSpan: number
  children: React.ReactNode
  py?: number
}) {
  return (
    <TableRow>
      <TableCell colSpan={colSpan} sx={{ py, color: 'text.secondary' }}>
        <Box sx={{ position: 'sticky', left: 0, display: 'inline-block' }}>{children}</Box>
      </TableCell>
    </TableRow>
  )
}

export function Loading() {
  return (
    <Box sx={{ display: 'flex', flexGrow: 1, justifyContent: 'center', alignItems: 'center', minHeight: '50vh' }}>
      <CircularProgress size={28} />
    </Box>
  )
}

/** Toast helper: one hook, one element, used by every page. */
export function useToast() {
  const [message, setMessage] = useState<string | null>(null)
  const [severity, setSeverity] = useState<'success' | 'error' | 'info'>('info')

  const show = useCallback((text: string, kind: 'success' | 'error' | 'info' = 'info') => {
    setSeverity(kind)
    setMessage(text)
  }, [])

  const element = (
    <Snackbar
      open={message !== null}
      autoHideDuration={severity === 'error' ? 8000 : 4000}
      onClose={() => setMessage(null)}
      anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
    >
      <Alert severity={severity} onClose={() => setMessage(null)} variant="filled">
        {message}
      </Alert>
    </Snackbar>
  )

  return { show, element }
}

export function formatTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function relativeTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts).getTime()
  if (Number.isNaN(d)) return '—'
  const seconds = Math.round((Date.now() - d) / 1000)
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}
