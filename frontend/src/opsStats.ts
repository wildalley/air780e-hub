/**
 * Derived figures for the operations stats card.
 *
 * Separate from `pages/Operations.tsx` so the page exports only its component.
 * The rounding rule here is the reason this is worth its own module and test:
 * it is a deliberate deviation from `toFixed`, and it must not be "tidied" back.
 */
import { STATUS } from './tokens'

/**
 * Success rate, or an em dash when nothing ran. 0/0 is not 0% — a window with
 * no attempts says nothing about health, and rendering it as 0% would light up
 * the accent colour on an idle hub.
 */
export function successRate(ok: number, failed: number): { text: string; accent?: string } {
  const total = ok + failed
  if (total === 0) return { text: '—' }
  const percent = (ok / total) * 100
  // Only a clean sweep prints "100%". With even one failure, `toFixed(1)`
  // would round 99.95%+ up to "100.0%" and hide it, so floor to the tenth
  // instead — a rate that is not perfect must never read as if it were.
  const text = failed === 0
    ? '100%'
    : `${(Math.floor(percent * 10) / 10).toFixed(1)}%`
  return {
    text,
    accent: percent >= 99 ? STATUS.good : percent >= 90 ? STATUS.warning : STATUS.critical,
  }
}
