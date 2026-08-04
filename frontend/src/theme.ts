import { createTheme, type Theme } from '@mui/material/styles'
import { VIZ, type Mode } from './tokens'

const FONT =
  'system-ui, -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif'

const EASE = 'cubic-bezier(0.16, 1, 0.3, 1)'

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
      // Display scale: tight leading + negative tracking as it grows (Apple §15).
      h1: { fontSize: '1.75rem', fontWeight: 650, letterSpacing: '-0.02em', lineHeight: 1.15 },
      h2: { fontSize: '1.25rem', fontWeight: 650, letterSpacing: '-0.01em', lineHeight: 1.3 },
      h3: { fontSize: '0.95rem', fontWeight: 600, letterSpacing: '-0.01em', lineHeight: 1.4 },
      button: { textTransform: 'none', fontWeight: 600, letterSpacing: '0.01em' },
      caption: { letterSpacing: '0.01em' },
    },
    shape: { borderRadius: 12 },
    components: {
      MuiPaper: {
        styleOverrides: {
          root: {
            backgroundImage: 'none',
            border: `1px solid ${viz.border}`,
            boxShadow: viz.shadowCard,
          },
        },
      },
      MuiCard: { defaultProps: { elevation: 0 } },
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: {
          root: {
            borderRadius: 10,
            // Feedback lives on the press (Apple §1): a physical push, instant.
            transition: `transform 140ms ${EASE}, background-color 160ms ${EASE}, border-color 160ms ${EASE}, color 160ms ${EASE}`,
            '&:active': { transform: 'scale(0.97)' },
          },
        },
      },
      MuiIconButton: {
        styleOverrides: {
          root: {
            transition: `transform 140ms ${EASE}, background-color 160ms ${EASE}`,
            '&:active': { transform: 'scale(0.92)' },
          },
        },
      },
      MuiChip: {
        styleOverrides: { root: { borderRadius: 999, fontWeight: 500 } },
      },
      MuiInputBase: {
        styleOverrides: {
          input: {
            '&:-webkit-autofill': {
              WebkitBoxShadow: `0 0 0 100px ${viz.surface} inset !important`,
              WebkitTextFillColor: `${viz.textPrimary} !important`,
              caretColor: viz.textPrimary,
              borderRadius: 'inherit',
            },
          },
        },
      },
      MuiTableCell: {
        styleOverrides: {
          root: { borderColor: viz.border },
          // Columns of numbers are the one place equal-width digits help.
          body: { fontVariantNumeric: 'tabular-nums' },
          // Headers carry hierarchy by weight and colour, not just size.
          head: {
            fontSize: '0.72rem',
            fontWeight: 600,
            color: viz.muted,
            letterSpacing: '0.02em',
            lineHeight: 1.4,
          },
        },
      },
      MuiListItemButton: {
        styleOverrides: {
          root: { transition: 'background-color 150ms ease' },
        },
      },
    },
  })
}
