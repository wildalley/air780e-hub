import { describe, expect, it } from 'vitest'
import { mostUrgentSimDeadline, simDeadlineStatus } from './simExpiry'

const TODAY = new Date(2026, 7, 16, 18, 30)

describe('simDeadlineStatus', () => {
  it('keeps dates outside the reminder window normal', () => {
    expect(simDeadlineStatus('2026-09-16', TODAY)).toEqual({
      days: 31,
      label: '31 天后到期',
      level: 'normal',
    })
  })

  it('warns at 30 days and becomes critical at 7 days', () => {
    expect(simDeadlineStatus('2026-09-15', TODAY).level).toBe('warning')
    expect(simDeadlineStatus('2026-09-15', TODAY).days).toBe(30)
    expect(simDeadlineStatus('2026-08-23', TODAY).level).toBe('critical')
    expect(simDeadlineStatus('2026-08-23', TODAY).days).toBe(7)
  })

  it('distinguishes today from an overdue plan', () => {
    expect(simDeadlineStatus('2026-08-16', TODAY).label).toBe('今天到期')
    expect(simDeadlineStatus('2026-08-15', TODAY).label).toBe('已过期 1 天')
  })

  it('handles an unset or malformed legacy value', () => {
    expect(simDeadlineStatus(null, TODAY).level).toBe('unset')
    expect(simDeadlineStatus('2026-02-30', TODAY)).toEqual({
      days: null,
      label: '到期日无效',
      level: 'invalid',
    })
  })
})

describe('mostUrgentSimDeadline', () => {
  it('prefers a critical keep-alive date over a package warning', () => {
    expect(mostUrgentSimDeadline('2026-09-15', '2026-08-21', TODAY)).toEqual({
      kind: 'activity',
      days: 5,
      label: '保号：剩余 5 天',
      level: 'critical',
    })
  })

  it('uses the closest date when both have the same severity', () => {
    expect(mostUrgentSimDeadline('2026-08-23', '2026-08-19', TODAY).kind).toBe(
      'activity',
    )
  })

  it('reports an unset lifecycle when neither date is configured', () => {
    expect(mostUrgentSimDeadline(null, null, TODAY)).toEqual({
      kind: null,
      days: null,
      label: '未设置期限',
      level: 'unset',
    })
  })
})
