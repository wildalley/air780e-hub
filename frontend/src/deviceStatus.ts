import type { Device, NetworkDiagnostic, NetworkDiagnostics } from './api'

export function radioStatus(device: Pick<Device, 'radio_enabled' | 'registered'>): string {
  if (device.radio_enabled == null) return '状态未知'
  if (!device.radio_enabled) return '飞行模式'
  return device.registered ? '射频开启 · 已注册' : '射频开启 · 正在注册'
}

export function networkRegistrationStatus(
  device: Pick<Device, 'registered' | 'eps_registered' | 'cs_registered'>,
): string {
  if (!device.registered) return '移动网络未注册'
  const domains: string[] = []
  if (device.eps_registered) domains.push('LTE')
  if (device.cs_registered) domains.push('CS')
  return domains.length > 0 ? `${domains.join(' + ')} 已注册` : '移动网络已注册'
}

export function imsRegistrationStatus(
  device: Pick<Device, 'ims_registered'>,
): string {
  if (device.ims_registered == null) return 'IMS 状态未知'
  return device.ims_registered ? 'IMS 已注册' : 'IMS 未注册'
}

const DIAGNOSTIC_COMMANDS: [keyof NetworkDiagnostics, string][] = [
  ['cced', 'AT+CCED'],
  ['eemginfo', 'AT+EEMGINFO'],
  ['bandind', 'AT*BANDIND?'],
  ['sysinfo', 'AT^SYSINFO'],
]

export function formatDiagnostics(diagnostics: NetworkDiagnostics): string {
  // A section absent from the payload is skipped rather than rendered empty: an
  // agent one version behind simply does not report the newer commands, which
  // is not the same as this firmware refusing them.
  const section = (name: string, value: NetworkDiagnostic | undefined) =>
    value ? [`[${name}]`, ...(value.lines.length ? value.lines : [value.error || '无返回'])] : []
  return DIAGNOSTIC_COMMANDS.flatMap(([key, name]) => section(name, diagnostics[key])).join('\n')
}
