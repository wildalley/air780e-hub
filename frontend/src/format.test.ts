import { afterEach, describe, expect, it, vi } from 'vitest'
import { formatTs, relativeTs } from './format'

afterEach(() => {
  vi.useRealTimers()
})

describe('formatTs', () => {
  it('renders an em dash for missing values', () => {
    // Every table in the UI feeds nullable timestamps through this, so the
    // empty cases matter as much as the happy one.
    expect(formatTs(null)).toBe('—')
    expect(formatTs(undefined)).toBe('—')
    expect(formatTs('')).toBe('—')
  })

  it('zero-pads to a stable width', () => {
    // Columns line up only if the output never varies in length.
    expect(formatTs('2026-01-02T03:04:05Z')).toHaveLength(16)
    expect(formatTs('2026-11-12T13:14:15Z')).toHaveLength(16)
  })

  it('returns the input unchanged when it is not a date', () => {
    expect(formatTs('not a date')).toBe('not a date')
  })
})

describe('relativeTs', () => {
  it('renders an em dash for missing or unparseable values', () => {
    expect(relativeTs(null)).toBe('—')
    expect(relativeTs(undefined)).toBe('—')
    expect(relativeTs('not a date')).toBe('—')
  })

  it('crosses each threshold at the right boundary', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-06T12:00:00Z'))
    const ago = (seconds: number) =>
      relativeTs(new Date(Date.now() - seconds * 1000).toISOString())

    expect(ago(0)).toBe('刚刚')
    expect(ago(59)).toBe('刚刚')
    expect(ago(60)).toBe('1 分钟前')
    expect(ago(3599)).toBe('59 分钟前')
    expect(ago(3600)).toBe('1 小时前')
    expect(ago(86_399)).toBe('23 小时前')
    expect(ago(86_400)).toBe('1 天前')
    expect(ago(86_400 * 30)).toBe('30 天前')
  })
})
