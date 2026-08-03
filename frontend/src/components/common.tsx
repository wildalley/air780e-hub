import { Alert, Box, Chip, CircularProgress, Snackbar } from '@mui/material'
import CheckIcon from '@mui/icons-material/CheckCircleOutline'
import OfflineIcon from '@mui/icons-material/CloudOffOutlined'
import { useCallback, useState } from 'react'
import { STATUS } from '../tokens'

/**
 * Small shared pieces.
 *
 * Status is never colour alone — every chip carries an icon and a word, which
 * is what keeps it readable under CVD and in forced-colors mode.
 */

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

export function Loading() {
  return (
    <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
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
