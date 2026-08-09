/**
 * Motion, and the preference that switches it off.
 *
 * Both pieces live together because neither is correct without the other: the
 * entrance is gated on the same preference the hook reads, so a change to one
 * that forgets the other reintroduces animation for people who asked for none.
 *
 * `useReducedMotion` currently has no caller — it was already unused before
 * these moved out of `components/common`. Kept because the CSS in
 * `entranceStyle` covers the declarative case and this covers the case where a
 * component has to *branch* in JS; reach for the CSS first.
 */
import { useMediaQuery } from '@mui/material'
import type { CSSObject } from '@mui/material/styles'

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
