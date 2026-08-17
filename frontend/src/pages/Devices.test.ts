import { describe, expect, it } from 'vitest'
import {
  imsRegistrationStatus,
  networkRegistrationStatus,
  radioStatus,
} from '../deviceStatus'

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

describe('networkRegistrationStatus', () => {
  it('shows the registration domain when a new agent reports it', () => {
    expect(
      networkRegistrationStatus({
        registered: 1,
        eps_registered: 1,
        cs_registered: 0,
      }),
    ).toBe('LTE 已注册')
    expect(
      networkRegistrationStatus({
        registered: 1,
        eps_registered: 1,
        cs_registered: 1,
      }),
    ).toBe('LTE + CS 已注册')
  })

  it('falls back cleanly for older agents', () => {
    expect(networkRegistrationStatus({ registered: 1 })).toBe('移动网络已注册')
    expect(networkRegistrationStatus({ registered: 0 })).toBe('移动网络未注册')
  })
})

describe('imsRegistrationStatus', () => {
  it('distinguishes unsupported or unknown from unregistered', () => {
    expect(imsRegistrationStatus({})).toBe('IMS 状态未知')
    expect(imsRegistrationStatus({ ims_registered: null })).toBe('IMS 状态未知')
    expect(imsRegistrationStatus({ ims_registered: 0 })).toBe('IMS 未注册')
    expect(imsRegistrationStatus({ ims_registered: 1 })).toBe('IMS 已注册')
  })
})
