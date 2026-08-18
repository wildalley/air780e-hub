import { describe, expect, it } from 'vitest'
import {
  formatDiagnostics,
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

describe('formatDiagnostics', () => {
  const ok = (line: string) => ({ lines: [line], error: null })

  it('labels every section with the command that produced it, parameters included', () => {
    // The label has to carry the parameters: AT+CCED has no bare execute form,
    // so a plain "AT+CCED" label would send anyone retyping it by hand into
    // +CME ERROR: 3 and read as a permission problem.
    expect(
      formatDiagnostics({
        cced: ok('+CCED:LTE current cell: 460,00'),
        cced_neighbors: ok('+CCED:LTE neighbor cell: 460,00'),
        eemginfo: ok('+EEMGINFO: LTE'),
        bandind: ok('*BANDIND: 0, 39, 7'),
        sysinfo: ok('^SYSINFO: 2,2,1,17,1,7'),
      }),
    ).toBe(
      [
        '[AT+CCED=0,1]',
        '+CCED:LTE current cell: 460,00',
        '[AT+CCED=0,2]',
        '+CCED:LTE neighbor cell: 460,00',
        '[AT+EEMGINFO]',
        '+EEMGINFO: LTE',
        '[AT*BANDIND?]',
        '*BANDIND: 0, 39, 7',
        '[AT^SYSINFO]',
        '^SYSINFO: 2,2,1,17,1,7',
      ].join('\n'),
    )
  })

  it('keeps a refusing firmware distinguishable from an agent that never asked', () => {
    // The refusal is worth showing — it is evidence about this firmware. A
    // section the agent does not report yet is not, so it is left out entirely.
    expect(
      formatDiagnostics({
        cced: ok('+CCED: 0,460'),
        eemginfo: { lines: [], error: '+CME ERROR 4 (operation not supported)' },
      }),
    ).toBe(
      [
        '[AT+CCED=0,1]',
        '+CCED: 0,460',
        '[AT+EEMGINFO]',
        '+CME ERROR 4 (operation not supported)',
      ].join('\n'),
    )
  })

  it('never renders a section as blank', () => {
    expect(formatDiagnostics({ cced: { lines: [], error: null }, eemginfo: ok('x') })).toBe(
      ['[AT+CCED=0,1]', '无返回', '[AT+EEMGINFO]', 'x'].join('\n'),
    )
  })
})
