/** Calendar helpers that follow the server's configured IANA timezone. */

function partsFor(date: Date, timezone?: string): { year: number; month: number; day: number } {
  if (Number.isNaN(date.getTime())) date = new Date()
  let formatter: Intl.DateTimeFormat
  const options = { year: 'numeric', month: 'numeric', day: 'numeric' } as const
  try {
    formatter = new Intl.DateTimeFormat('en-US', { ...options, timeZone: timezone })
  } catch {
    formatter = new Intl.DateTimeFormat('en-US', { ...options, timeZone: 'UTC' })
  }
  const values = Object.fromEntries(
    formatter.formatToParts(date)
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, Number(part.value)]),
  )
  return { year: values.year, month: values.month, day: values.day }
}

function keyOf({ year, month, day }: { year: number; month: number; day: number }): string {
  return `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

/** Return local calendar keys ending with the current server-calendar day. */
export function calendarDayKeys(days: number, timezone?: string, now = new Date()): string[] {
  if (!Number.isInteger(days) || days < 1) return []
  const today = partsFor(now, timezone)
  // Once the local date is known, UTC noon is only used as a date arithmetic
  // carrier. We never format it back through a timezone, so DST cannot shift
  // a key while walking backwards.
  const cursor = new Date(Date.UTC(today.year, today.month - 1, today.day, 12))
  const keys: string[] = []
  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const date = new Date(cursor)
    date.setUTCDate(cursor.getUTCDate() - offset)
    keys.push(keyOf({ year: date.getUTCFullYear(), month: date.getUTCMonth() + 1, day: date.getUTCDate() }))
  }
  return keys
}

export function shortCalendarDay(iso: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso)
  return match ? `${Number(match[2])}/${Number(match[3])}` : iso
}
