import { Box, Card, CardContent, Typography } from '@mui/material'
import type { ReactNode } from 'react'

/**
 * Stat tile — the right form for a headline number.
 *
 * A single current value is not a one-bar bar chart. Label, value, optional
 * supporting note. Values use the font's proportional figures: `tabular-nums`
 * would give every digit the width of a zero and make a number like 121 look
 * loose at this size.
 */

export interface StatTileProps {
  label: string
  value: number | string
  note?: ReactNode
  /** A colored dot beside the value. Identity/state never rides on the text itself. */
  accent?: string
  compact?: boolean
}

function formatValue(value: number | string, compact: boolean): string {
  if (typeof value === 'string') return value
  if (!compact) return value.toLocaleString()
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 10_000) return `${(value / 1000).toFixed(1)}K`
  return value.toLocaleString()
}

export function StatTile({ label, value, note, accent, compact = true }: StatTileProps) {
  return (
    <Card sx={{ height: '100%' }}>
      <CardContent sx={{ py: 2.5 }}>
        <Typography variant="body2" color="text.secondary" gutterBottom>
          {label}
        </Typography>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          {accent && (
            <Box
              aria-hidden
              sx={{
                width: 10,
                height: 10,
                borderRadius: '50%',
                bgcolor: accent,
                flexShrink: 0,
              }}
            />
          )}
          <Typography
            component="div"
            sx={{ fontSize: '2rem', fontWeight: 600, lineHeight: 1.1 }}
          >
            {formatValue(value, compact)}
          </Typography>
        </Box>
        {note && (
          <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5, display: 'block' }}>
            {note}
          </Typography>
        )}
      </CardContent>
    </Card>
  )
}
