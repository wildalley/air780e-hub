import { Box, Card, CardContent, Stack, Typography } from '@mui/material'
import type { ReactNode } from 'react'

/**
 * Stat tile — the right form for a headline number.
 *
 * A single current value is not a one-bar bar chart. Label, value, optional
 * supporting note, and an optional identity icon. Values use the font's
 * proportional figures: `tabular-nums` would give every digit the width of a
 * zero and make a number like 121 look loose at this size.
 */

export interface StatTileProps {
  label: string
  value: number | string
  note?: ReactNode
  /** A colored dot beside the value. Identity/state never rides on the text itself. */
  accent?: string
  /** Identity glyph in a tinted square, when the tile benefits from one. */
  icon?: ReactNode
  compact?: boolean
}

function formatValue(value: number | string, compact: boolean): string {
  if (typeof value === 'string') return value
  if (!compact) return value.toLocaleString()
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 10_000) return `${(value / 1000).toFixed(1)}K`
  return value.toLocaleString()
}

export function StatTile({ label, value, note, accent, icon, compact = true }: StatTileProps) {
  return (
    <Card sx={{ height: '100%' }}>
      <CardContent sx={{ py: 2.5, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-start" spacing={2}>
          <Typography variant="body2" color="text.secondary">
            {label}
          </Typography>
          {icon && (
            <Box
              aria-hidden
              sx={{
                width: 40,
                height: 40,
                borderRadius: 2,
                flexShrink: 0,
                display: 'grid',
                placeItems: 'center',
                bgcolor: 'action.hover',
                color: 'primary.main',
              }}
            >
              {icon}
            </Box>
          )}
        </Stack>
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
            sx={{ fontSize: '2.1rem', fontWeight: 650, lineHeight: 1.05, letterSpacing: '-0.02em' }}
          >
            {formatValue(value, compact)}
          </Typography>
        </Box>
        {note && (
          <Typography variant="caption" color="text.secondary">
            {note}
          </Typography>
        )}
      </CardContent>
    </Card>
  )
}
