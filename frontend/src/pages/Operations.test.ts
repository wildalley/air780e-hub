import { successRate } from '../opsStats'
import { describe, expect, it } from 'vitest'
import { STATUS } from '../tokens'

/**
 * The operations card reads these straight off the diagnostics payload, and an
 * idle hub is the common case — most of what matters here is the empty window
 * and the rounding boundary, not the happy path.
 */
describe('successRate', () => {
  it('renders an em dash when nothing ran', () => {
    // 0/0 is not 0%. A window with no attempts says nothing about health, and
    // 0% would paint the critical accent on a hub that is merely quiet.
    const rate = successRate(0, 0)
    expect(rate.text).toBe('—')
    expect(rate.accent).toBeUndefined()
  })

  it('keeps the denominator visible in the thresholds', () => {
    expect(successRate(1, 0).text).toBe('100%')
    expect(successRate(900, 100).text).toBe('90.0%')
    expect(successRate(0, 5).text).toBe('0.0%')
  })

  it('never rounds a window with a failure up to 100%', () => {
    // 1999/2000 is exactly 99.95%, which toFixed(1) prints as "100.0%" — a
    // rounded-up perfect score hides the one send that failed. Only a clean
    // sweep is allowed to read as 100.
    expect(successRate(1999, 1).text).toBe('99.9%')
    expect(successRate(19999, 1).text).toBe('99.9%')
    expect(successRate(2000, 0).text).toBe('100%')
  })

  it('escalates the accent as the rate drops', () => {
    expect(successRate(100, 0).accent).toBe(STATUS.good)
    expect(successRate(99, 1).accent).toBe(STATUS.good)
    expect(successRate(95, 5).accent).toBe(STATUS.warning)
    expect(successRate(80, 20).accent).toBe(STATUS.critical)
  })
})
