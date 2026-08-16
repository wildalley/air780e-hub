import { describe, expect, it } from 'vitest'
import { simBalanceStatus } from './simBalance'

describe('simBalanceStatus', () => {
  it('stays hidden until a threshold is configured', () => {
    expect(simBalanceStatus('12.50', null, 'GBP')).toBeNull()
  })

  it('distinguishes healthy, low, and exhausted balances', () => {
    expect(simBalanceStatus('12.50', '10.00', 'gbp')).toEqual({
      label: '余额：GBP 12.50',
      level: 'normal',
    })
    expect(simBalanceStatus('10.00', '10.00', 'GBP')).toEqual({
      label: '余额偏低：GBP 10.00',
      level: 'warning',
    })
    expect(simBalanceStatus('0.00', '10.00', 'GBP')?.level).toBe('critical')
    expect(simBalanceStatus('-0.01', '10.00', 'GBP')?.level).toBe('critical')
  })

  it('does not lose precision above Number.MAX_SAFE_INTEGER', () => {
    expect(
      simBalanceStatus('9007199254740993.000001', '9007199254740993', 'USD')?.level,
    ).toBe('normal')
  })

  it('shows missing or invalid manually maintained data', () => {
    expect(simBalanceStatus(null, '5.00', 'EUR')).toEqual({
      label: '余额：未记录',
      level: 'unavailable',
    })
    expect(simBalanceStatus('bad', '5.00', 'EUR')?.level).toBe('invalid')
    expect(simBalanceStatus('5.00', '-1.00', 'EUR')?.level).toBe('invalid')
  })
})
