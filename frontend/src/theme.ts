import { createTheme, type Theme } from '@mui/material/styles'
import { VIZ, type Mode } from './tokens'

const FONT = 'system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'

export function buildTheme(mode: Mode): Theme {
  const viz = VIZ[mode]
  return createTheme({
    palette: {
      mode,
      primary: { main: viz.series[0] },
      background: { default: viz.plane, paper: viz.surface },
      text: { primary: viz.textPrimary, secondary: viz.textSecondary },
      divider: viz.border,
    },
    typography: {
      fontFamily: FONT,
      h1: { fontSize: '1.5rem', fontWeight: 600 },
      h2: { fontSize: '1.25rem', fontWeight: 600 },
      h3: { fontSize: '1rem', fontWeight: 600 },
      button: { textTransform: 'none' },
    },
    shape: { borderRadius: 10 },
    components: {
      MuiPaper: {
        styleOverrides: {
          root: { backgroundImage: 'none', border: `1px solid ${viz.border}` },
        },
      },
      MuiCard: { defaultProps: { elevation: 0 } },
      MuiButton: { defaultProps: { disableElevation: true } },
      MuiTableCell: {
        styleOverrides: {
          root: { borderColor: viz.border },
          // Columns of numbers are the one place equal-width digits help.
          body: { fontVariantNumeric: 'tabular-nums' },
        },
      },
    },
  })
}
