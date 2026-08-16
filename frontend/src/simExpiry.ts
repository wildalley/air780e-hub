export type SimDeadlineLevel = 'unset' | 'normal' | 'warning' | 'critical' | 'invalid'

export interface SimDeadlineStatus {
  days: number | null
  label: string
  level: SimDeadlineLevel
}

export interface SimLifecycleStatus extends SimDeadlineStatus {
  kind: 'package' | 'activity' | null
}

const DAY_MS = 24 * 60 * 60 * 1000
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/

function isoDay(value: string): number | null {
  const match = ISO_DATE.exec(value)
  if (!match) return null

  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const parsed = new Date(0)
  parsed.setUTCHours(0, 0, 0, 0)
  parsed.setUTCFullYear(year, month - 1, day)
  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day
  ) {
    return null
  }
  return parsed.getTime()
}

export function simDeadlineStatus(
  deadlineAt: string | null,
  now: Date = new Date(),
): SimDeadlineStatus {
  if (!deadlineAt) return { days: null, label: '未设置到期日', level: 'unset' }

  const expiry = isoDay(deadlineAt)
  if (expiry === null) return { days: null, label: '到期日无效', level: 'invalid' }

  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())
  const days = Math.round((expiry - today) / DAY_MS)
  if (days < 0) return { days, label: `已过期 ${-days} 天`, level: 'critical' }
  if (days === 0) return { days, label: '今天到期', level: 'critical' }
  if (days <= 7) return { days, label: `剩余 ${days} 天`, level: 'critical' }
  if (days <= 30) return { days, label: `剩余 ${days} 天`, level: 'warning' }
  return { days, label: `${days} 天后到期`, level: 'normal' }
}

const LEVEL_PRIORITY: Record<SimDeadlineLevel, number> = {
  unset: 0,
  normal: 1,
  warning: 2,
  critical: 3,
  invalid: 4,
}

/** Return the one lifecycle date that deserves the operator's attention first. */
export function mostUrgentSimDeadline(
  expiresAt: string | null,
  activityDueAt: string | null,
  now: Date = new Date(),
): SimLifecycleStatus {
  const candidates = [
    expiresAt
      ? { kind: 'package' as const, prefix: '套餐', ...simDeadlineStatus(expiresAt, now) }
      : null,
    activityDueAt
      ? {
          kind: 'activity' as const,
          prefix: '保号',
          ...simDeadlineStatus(activityDueAt, now),
        }
      : null,
  ].filter((candidate): candidate is NonNullable<typeof candidate> => candidate !== null)

  if (candidates.length === 0) {
    return { kind: null, days: null, label: '未设置期限', level: 'unset' }
  }

  candidates.sort((left, right) => {
    const level = LEVEL_PRIORITY[right.level] - LEVEL_PRIORITY[left.level]
    if (level !== 0) return level
    return (left.days ?? Number.POSITIVE_INFINITY) - (right.days ?? Number.POSITIVE_INFINITY)
  })
  const selected = candidates[0]
  return {
    kind: selected.kind,
    days: selected.days,
    label: `${selected.prefix}：${selected.label}`,
    level: selected.level,
  }
}
