/**
 * Visualization tokens.
 *
 * Both columns are *selected* for their surface, not derived by flipping the
 * light values. The categorical order is fixed: a series keeps its hue when
 * other series are filtered out, so a reader who learned "module A is blue"
 * is never misled by a repaint.
 *
 * The two-series pair used by the signal chart was checked with the palette
 * validator against both surfaces:
 *   light  worst adjacent CVD ΔE 24.7 (protan), normal-vision ΔE 33.6
 *   dark   worst adjacent CVD ΔE 26.8 (protan), normal-vision ΔE 31.8
 * Both clear the ΔE ≥ 8 CVD gate and the ≥ 15 normal-vision floor with room
 * to spare. Re-run the validator before changing any hex here.
 */

export type Mode = 'light' | 'dark'

/** Fixed categorical order — assign by index, never cycle past the end. */
export const SERIES: Record<Mode, string[]> = {
  light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'],
  dark: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
}

/** Reserved for state. Never reused as "series 4". Always paired with a label. */
export const STATUS = {
  good: '#0ca30c',
  warning: '#fab219',
  serious: '#ec835a',
  critical: '#d03b3b',
} as const

/** Single-hue ramp for magnitude (the storage meter's track and fill). */
export const BLUE_RAMP = {
  100: '#cde2fb',
  200: '#9ec5f4',
  250: '#86b6ef',
  400: '#3987e5',
  450: '#2a78d6',
  550: '#1c5cab',
  600: '#184f95',
} as const

export interface VizTokens {
  surface: string
  plane: string
  textPrimary: string
  textSecondary: string
  muted: string
  gridline: string
  axis: string
  border: string
  series: string[]
}

export const VIZ: Record<Mode, VizTokens> = {
  light: {
    surface: '#fcfcfb',
    plane: '#f9f9f7',
    textPrimary: '#0b0b0b',
    textSecondary: '#52514e',
    muted: '#898781',
    gridline: '#e1e0d9',
    axis: '#c3c2b7',
    border: 'rgba(11,11,11,0.10)',
    series: SERIES.light,
  },
  dark: {
    surface: '#1a1a19',
    plane: '#0d0d0d',
    textPrimary: '#ffffff',
    textSecondary: '#c3c2b7',
    muted: '#898781',
    gridline: '#2c2c2a',
    axis: '#383835',
    border: 'rgba(255,255,255,0.10)',
    series: SERIES.dark,
  },
}

/** Stable hue per entity: index into the fixed order, by first-seen position. */
export function seriesColor(mode: Mode, index: number): string {
  const palette = SERIES[mode]
  // Past the token ceiling the honest answer is to fold into "Other" rather
  // than generate a 9th hue that no CVD reader could separate.
  return palette[Math.min(index, palette.length - 1)]
}

/** Meter severity: accent until it matters, then warning, then danger. */
export function meterColor(ratio: number): string {
  if (ratio >= 0.9) return STATUS.critical
  if (ratio >= 0.7) return STATUS.warning
  return BLUE_RAMP[450]
}
