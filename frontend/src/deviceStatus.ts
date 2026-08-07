import type { Device } from './api'

export function radioStatus(device: Pick<Device, 'radio_enabled' | 'registered'>): string {
  if (device.radio_enabled == null) return '状态未知'
  if (!device.radio_enabled) return '飞行模式'
  return device.registered ? '射频开启 · 已注册' : '射频开启 · 正在注册'
}
