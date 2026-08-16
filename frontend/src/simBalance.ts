export type SimBalanceLevel = 'normal' | 'warning' | 'critical' | 'unavailable' | 'invalid'

export interface SimBalanceStatus {
  label: string
  level: SimBalanceLevel
}

const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/

function compareDecimal(left: string, right: string): number | null {
  const leftMatch = DECIMAL.exec(left)
  const rightMatch = DECIMAL.exec(right)
  if (!leftMatch || !rightMatch) return null

  const scale = Math.max(leftMatch[3]?.length ?? 0, rightMatch[3]?.length ?? 0)
  const scaled = (match: RegExpExecArray) => {
    const fraction = (match[3] ?? '').padEnd(scale, '0')
    const magnitude = BigInt(`${match[2]}${fraction}`)
    return match[1] === '-' ? -magnitude : magnitude
  }
  const leftValue = scaled(leftMatch)
  const rightValue = scaled(rightMatch)
  if (leftValue < rightValue) return -1
  if (leftValue > rightValue) return 1
  return 0
}

/** Build the optional status chip shown when a low-balance threshold is set. */
export function simBalanceStatus(
  balance: string | null,
  threshold: string | null,
  currency: string,
): SimBalanceStatus | null {
  if (!threshold) return null
  if (!balance) return { label: '余额：未记录', level: 'unavailable' }

  const thresholdToZero = compareDecimal(threshold, '0')
  const balanceToZero = compareDecimal(balance, '0')
  const balanceToThreshold = compareDecimal(balance, threshold)
  if (
    thresholdToZero === null ||
    thresholdToZero < 0 ||
    balanceToZero === null ||
    balanceToThreshold === null
  ) {
    return { label: '余额格式无效', level: 'invalid' }
  }

  const amount = `${currency.trim().toUpperCase()} ${balance}`.trim()
  if (balanceToZero <= 0) return { label: `余额：${amount}`, level: 'critical' }
  if (balanceToThreshold <= 0) {
    return { label: `余额偏低：${amount}`, level: 'warning' }
  }
  return { label: `余额：${amount}`, level: 'normal' }
}
