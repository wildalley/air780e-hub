import { describe, expect, it } from 'vitest'
import { radioStatus } from '../deviceStatus'

describe('radioStatus', () => {
  it('keeps an older or not-yet-ready agent honest', () => {
    expect(radioStatus({ radio_enabled: null, registered: 0 })).toBe('状态未知')
  })

  it('distinguishes flight mode from registration in progress', () => {
    expect(radioStatus({ radio_enabled: 0, registered: 0 })).toBe('飞行模式')
    expect(radioStatus({ radio_enabled: 1, registered: 0 })).toBe('射频开启 · 正在注册')
    expect(radioStatus({ radio_enabled: 1, registered: 1 })).toBe('射频开启 · 已注册')
  })
})
