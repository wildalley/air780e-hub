import { describe, expect, it } from 'vitest'
import { formatVoltage, supplyVoltageStatus } from './supplyVoltage'

describe('formatVoltage', () => {
  it('renders millivolts as volts', () => {
    expect(formatVoltage(3968)).toBe('3.97 V')
    expect(formatVoltage(4000)).toBe('4.00 V')
  })
})

describe('supplyVoltageStatus', () => {
  it('says nothing when the module reported no reading', () => {
    expect(supplyVoltageStatus(null, 3500)).toBeNull()
    expect(supplyVoltageStatus(undefined, 3500)).toBeNull()
  })

  it('shows a reading that has no threshold to be judged against', () => {
    expect(supplyVoltageStatus(3968, null)).toEqual({
      reading: '3.97 V',
      label: '供电 3.97 V',
      level: 'normal',
    })
  })

  it('separates healthy, low, and below-spec supplies', () => {
    expect(supplyVoltageStatus(3968, 3500)?.level).toBe('normal')
    // At the threshold is still healthy; the Agent alerts strictly below it.
    expect(supplyVoltageStatus(3500, 3500)?.level).toBe('normal')
    expect(supplyVoltageStatus(3499, 3500)).toEqual({
      reading: '3.50 V',
      label: '供电偏低 3.50 V',
      level: 'warning',
    })
    expect(supplyVoltageStatus(3299, 3500)).toEqual({
      reading: '3.30 V',
      label: '供电过低 3.30 V',
      level: 'critical',
    })
  })
})
