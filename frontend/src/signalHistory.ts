import type { StatusPoint } from './api'

export type RegistrationState = 'registered' | 'unregistered' | 'offline' | 'unknown'

export type RegistrationSample = Pick<StatusPoint, 'ts' | 'online' | 'registered'>

export interface RegistrationInterval {
  start: number
  end: number
  state: RegistrationState
}

export interface RegistrationSummary {
  unregisteredCount: number
  unregisteredMs: number
  offlineCount: number
  offlineMs: number
  unknownMs: number
}

export const REGISTRATION_LABEL: Record<RegistrationState, string> = {
  registered: '已注册',
  unregistered: '未注册',
  offline: '离线',
  unknown: '未知',
}

export function registrationState(
  sample: Pick<RegistrationSample, 'online' | 'registered'>,
): RegistrationState {
  if (!sample.online) return 'offline'
  return sample.registered ? 'registered' : 'unregistered'
}

/**
 * Turn status samples into continuous intervals for one selected history range.
 * A reported state remains valid until the next report. Before the first report
 * in a range the state is unknown, unless a sample at or before the boundary is
 * available.
 */
export function buildRegistrationIntervals(
  samples: readonly RegistrationSample[],
  rangeStart: number,
  rangeEnd: number,
): RegistrationInterval[] {
  if (!Number.isFinite(rangeStart) || !Number.isFinite(rangeEnd) || rangeEnd <= rangeStart) {
    return []
  }

  const byTime = new Map<number, RegistrationState>()
  for (const sample of samples) {
    const time = new Date(sample.ts).getTime()
    if (Number.isFinite(time) && time <= rangeEnd) {
      byTime.set(time, registrationState(sample))
    }
  }
  const ordered = [...byTime.entries()].sort(([a], [b]) => a - b)

  let state: RegistrationState = 'unknown'
  let cursor = rangeStart
  let index = 0
  while (index < ordered.length && ordered[index][0] <= rangeStart) {
    state = ordered[index][1]
    index += 1
  }

  const intervals: RegistrationInterval[] = []
  const append = (start: number, end: number, nextState: RegistrationState) => {
    if (end <= start) return
    const previous = intervals.at(-1)
    if (previous && previous.state === nextState && previous.end === start) {
      previous.end = end
      return
    }
    intervals.push({ start, end, state: nextState })
  }

  for (; index < ordered.length; index += 1) {
    const [time, nextState] = ordered[index]
    if (time >= rangeEnd) break
    append(cursor, time, state)
    cursor = time
    state = nextState
  }
  append(cursor, rangeEnd, state)
  return intervals
}

export function summarizeRegistration(
  intervals: readonly RegistrationInterval[],
): RegistrationSummary {
  const summary: RegistrationSummary = {
    unregisteredCount: 0,
    unregisteredMs: 0,
    offlineCount: 0,
    offlineMs: 0,
    unknownMs: 0,
  }

  for (const interval of intervals) {
    const duration = Math.max(0, interval.end - interval.start)
    if (interval.state === 'unregistered') {
      summary.unregisteredCount += 1
      summary.unregisteredMs += duration
    } else if (interval.state === 'offline') {
      summary.offlineCount += 1
      summary.offlineMs += duration
    } else if (interval.state === 'unknown') {
      summary.unknownMs += duration
    }
  }
  return summary
}

export function formatRegistrationDuration(milliseconds: number): string {
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return '0 分钟'
  if (milliseconds < 60_000) return '<1 分钟'

  const totalMinutes = Math.max(1, Math.round(milliseconds / 60_000))
  const days = Math.floor(totalMinutes / (24 * 60))
  const hours = Math.floor((totalMinutes % (24 * 60)) / 60)
  const minutes = totalMinutes % 60
  const parts: string[] = []
  if (days) parts.push(`${days} 天`)
  if (hours) parts.push(`${hours} 小时`)
  if (minutes || parts.length === 0) parts.push(`${minutes} 分钟`)
  return parts.join(' ')
}
