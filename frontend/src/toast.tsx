/**
 * Toast helper: one hook, one element, used by every page.
 *
 * `.tsx` because the hook hands back a ready-made element rather than asking
 * each caller to render a Snackbar correctly. It exports no component, so it
 * stays clear of the mixed-exports rule that Fast Refresh cares about.
 */
import { Alert, Snackbar } from '@mui/material'
import { useCallback, useState } from 'react'

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
