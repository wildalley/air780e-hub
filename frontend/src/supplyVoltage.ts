export type SupplyVoltageLevel = 'normal' | 'warning' | 'critical'

export interface SupplyVoltageStatus {
  /** Millivolts rendered as volts, e.g. "3.97 V". */
  reading: string
  label: string
  level: SupplyVoltageLevel
}

/**
 * Below the EC618's own nominal floor the module still runs, but a transmit
 * burst can brown it out — which presents as random unregistrations rather than
 * as a power problem. Kept in step with VOLTAGE_CRITICAL_MV in the Server's
 * gateway, which decides the incident severity.
 */
export const VOLTAGE_CRITICAL_MV = 3300

/** Millivolts as volts, at the two decimals the modules actually resolve. */
export function formatVoltage(millivolts: number): string {
  return `${(millivolts / 1000).toFixed(2)} V`
}

/**
 * Describe a module's supply reading for the device page.
 *
 * Returns null when there is nothing to say: a firmware that refuses `AT+CBC`
 * and an Agent too old to report the field are indistinguishable here, and
 * neither should show as a fault. A reading with no threshold is still worth
 * displaying — it just cannot be judged, so it stays `normal`.
 */
export function supplyVoltageStatus(
  millivolts: number | null | undefined,
  threshold: number | null | undefined,
): SupplyVoltageStatus | null {
  if (millivolts == null || !Number.isFinite(millivolts)) return null

  const reading = formatVoltage(millivolts)
  if (!threshold || !Number.isFinite(threshold) || millivolts >= threshold) {
    return { reading, label: `供电 ${reading}`, level: 'normal' }
  }
  if (millivolts < VOLTAGE_CRITICAL_MV) {
    return { reading, label: `供电过低 ${reading}`, level: 'critical' }
  }
  return { reading, label: `供电偏低 ${reading}`, level: 'warning' }
}
