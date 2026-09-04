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

export function packetDataStatus(
  device: Pick<Device, 'data_attached' | 'pdp_active'>,
): string {
  if (device.data_attached === 0 && device.pdp_active === 0) return '已关闭'
  if (device.pdp_active === 1) return 'PDP 已激活'
  if (device.data_attached === 1) return '已附着 · 未激活 PDP'
  if (device.data_attached === 0 && device.pdp_active == null) return 'PDP 未激活 · 附着未知'
  if (device.data_attached == null && device.pdp_active === 0) return '未激活 · 附着未知'
  return '状态未知'
}

export function roamingStatus(device: Pick<Device, 'roaming'>): string {
  if (device.roaming == null) return '漫游状态未知'
  return device.roaming ? '当前为漫游网络' : '当前为本地网络'
}

// Labelled with the exact command sent, parameters included: AT+CCED has no
// bare execute form, so a plain "AT+CCED" label would misdescribe the read and
// send anyone reproducing it by hand straight into +CME ERROR: 3.
const DIAGNOSTIC_COMMANDS: [keyof NetworkDiagnostics, string][] = [
  ['cced', 'AT+CCED=0,1'],
  ['cced_neighbors', 'AT+CCED=0,2'],
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
