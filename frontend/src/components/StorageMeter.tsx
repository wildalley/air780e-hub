import { Box, LinearProgress, Tooltip, Typography } from '@mui/material'
import { BLUE_RAMP, meterColor } from '../tokens'

/**
 * Meter — a single ratio against a limit.
 *
 * Not a two-slice pie. The fill carries severity (accent → warning → danger)
 * and the unfilled track is a lighter step of the *same* ramp, so state reads
 * across the whole bar rather than only at the boundary.
 *
 * This particular meter earns its place: a full message store makes the
 * network drop incoming SMS silently, so "nearly full" is the one number on
 * the dashboard worth alarming on.
 */

export interface StorageMeterProps {
  used: number
  capacity: number
  label?: string
}

export function StorageMeter({ used, capacity, label = '模块存储' }: StorageMeterProps) {
  const ratio = capacity > 0 ? Math.min(used / capacity, 1) : 0
  const color = meterColor(ratio)
  const severe = ratio >= 0.7

  return (
    <Box>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 0.5 }}>
        <Typography variant="body2" sx={{
          color: 'text.secondary'
        }}>
          {label}
        </Typography>
        <Typography
          variant="body2"
          sx={{
            color: 'text.secondary',
            fontVariantNumeric: 'tabular-nums'
          }}>
          {capacity > 0 ? `${used} / ${capacity}` : '未知'}
        </Typography>
      </Box>
      <Tooltip
        title={
          severe
            ? '存储接近上限时,新短信可能被静默丢弃 —— agent 会自动清空,持续告警说明清理跟不上'
            : `已用 ${Math.round(ratio * 100)}%`
        }
      >
        <LinearProgress
          variant="determinate"
          value={ratio * 100}
          aria-label={`${label} ${used} / ${capacity}`}
          sx={{
            height: 8,
            borderRadius: 4,
            backgroundColor: BLUE_RAMP[100],
            '& .MuiLinearProgress-bar': { backgroundColor: color, borderRadius: 4 },
          }}
        />
      </Tooltip>
      {severe && (
        <Typography variant="caption" sx={{ color, mt: 0.5, display: 'block' }}>
          {/* Status colour never carries the meaning alone — the words do. */}
          {ratio >= 0.9 ? '存储告急,可能开始丢短信' : '存储偏高'}
        </Typography>
      )}
    </Box>
  )
}
