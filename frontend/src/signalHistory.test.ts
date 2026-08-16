import { describe, expect, it } from 'vitest'
import {
  buildRegistrationIntervals,
  formatRegistrationDuration,
  registrationState,
  summarizeRegistration,
  type RegistrationSample,
} from './signalHistory'

const minute = 60_000
const start = Date.parse('2026-08-16T00:00:00Z')

function sample(offsetMinutes: number, online: number, registered: number): RegistrationSample {
  return {
    ts: new Date(start + offsetMinutes * minute).toISOString(),
    online,
    registered,
  }
}

describe('registration history', () => {
  it('marks the entire range unknown when there are no samples', () => {
    const intervals = buildRegistrationIntervals([], start, start + 30 * minute)

    expect(intervals).toEqual([
      { start, end: start + 30 * minute, state: 'unknown' },
    ])
    expect(summarizeRegistration(intervals).unknownMs).toBe(30 * minute)
  })

  it('keeps the leading unknown period and merges repeated states', () => {
    const intervals = buildRegistrationIntervals(
      [sample(5, 1, 1), sample(10, 1, 1), sample(15, 1, 0), sample(20, 1, 1)],
      start,
      start + 30 * minute,
    )

    expect(intervals).toEqual([
      { start, end: start + 5 * minute, state: 'unknown' },
      { start: start + 5 * minute, end: start + 15 * minute, state: 'registered' },
      { start: start + 15 * minute, end: start + 20 * minute, state: 'unregistered' },
      { start: start + 20 * minute, end: start + 30 * minute, state: 'registered' },
    ])
  })

  it('uses a sample before the boundary and clips later samples to the range', () => {
    const intervals = buildRegistrationIntervals(
      [sample(-5, 1, 1), sample(10, 0, 0), sample(40, 1, 1)],
      start,
      start + 30 * minute,
    )

    expect(intervals).toEqual([
      { start, end: start + 10 * minute, state: 'registered' },
      { start: start + 10 * minute, end: start + 30 * minute, state: 'offline' },
    ])
  })

  it('lets offline take precedence over the registration flag', () => {
    expect(registrationState(sample(0, 0, 1))).toBe('offline')
    expect(registrationState(sample(0, 1, 0))).toBe('unregistered')
    expect(registrationState(sample(0, 1, 1))).toBe('registered')
  })

  it('counts distinct outages and totals their durations', () => {
    const intervals = buildRegistrationIntervals(
      [
        sample(0, 1, 1),
        sample(5, 1, 0),
        sample(8, 1, 1),
        sample(12, 1, 0),
        sample(14, 0, 0),
        sample(18, 1, 1),
      ],
      start,
      start + 20 * minute,
    )

    expect(summarizeRegistration(intervals)).toEqual({
      unregisteredCount: 2,
      unregisteredMs: 5 * minute,
      offlineCount: 1,
      offlineMs: 4 * minute,
      unknownMs: 0,
    })
  })

  it('formats short and long durations without false precision', () => {
    expect(formatRegistrationDuration(0)).toBe('0 分钟')
    expect(formatRegistrationDuration(30_000)).toBe('<1 分钟')
    expect(formatRegistrationDuration(65 * minute)).toBe('1 小时 5 分钟')
    expect(formatRegistrationDuration((24 * 60 + 120) * minute)).toBe('1 天 2 小时')
  })
})
