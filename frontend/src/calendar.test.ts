import { describe, expect, it } from 'vitest'
import { calendarDayKeys, shortCalendarDay } from './calendar'

describe('calendarDayKeys', () => {
  it('walks days in the configured timezone across a DST change', () => {
    const now = new Date('2024-03-10T12:00:00Z')
    expect(calendarDayKeys(3, 'America/New_York', now)).toEqual([
      '2024-03-08', '2024-03-09', '2024-03-10',
    ])
  })

  it('uses a stable date key even when the browser has another timezone', () => {
    const now = new Date('2026-01-01T00:30:00Z')
    expect(calendarDayKeys(2, 'Asia/Shanghai', now)).toEqual([
      '2025-12-31', '2026-01-01',
    ])
    expect(calendarDayKeys(2, 'America/Los_Angeles', now)).toEqual([
      '2025-12-30', '2025-12-31',
    ])
  })
})

describe('shortCalendarDay', () => {
  it('formats a server date without parsing it in browser local time', () => {
    expect(shortCalendarDay('2026-01-02')).toBe('1/2')
    expect(shortCalendarDay('unknown')).toBe('unknown')
  })
})
