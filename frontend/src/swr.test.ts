/**
 * The shared SWR policy.
 *
 * `onErrorRetry` is the part worth pinning down: it decides whether a failed
 * request is tried again, and getting it wrong is invisible in the UI. A 401
 * that retries hammers the server and writes three audit rows per lapsed
 * session; a 503 that does not retry leaves a page blank until the user
 * reloads.
 */
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { SWR_OPTIONS } from './swr'

/**
 * Call `onErrorRetry` the way SWR does. The signature wants fully-resolved
 * config and revalidator objects; the policy reads only the four fields below,
 * so the casts keep the test to what it is actually about.
 */
function fire(error: unknown, retryCount: number, revalidate = vi.fn()) {
  SWR_OPTIONS.onErrorRetry?.(
    error as Error,
    'key',
    SWR_OPTIONS as never,
    revalidate as never,
    { retryCount } as never,
  )
  return revalidate
}

/** Drive `onErrorRetry` and report whether it scheduled another attempt. */
function retriesOn(error: unknown, retryCount = 0): boolean {
  vi.useFakeTimers()
  try {
    const revalidate = fire(error, retryCount)
    // Any backoff this policy picks is well inside a minute.
    vi.advanceTimersByTime(120_000)
    return revalidate.mock.calls.length > 0
  } finally {
    vi.useRealTimers()
  }
}

describe('SWR_OPTIONS.onErrorRetry', () => {
  it('does not retry a lapsed session', () => {
    // A 401 will answer 401 until the user logs in again. Retrying spends
    // three more requests and three audit rows to learn nothing.
    expect(retriesOn(new ApiError(401, 'not authenticated'))).toBe(false)
  })

  it('does not retry client errors that cannot change', () => {
    for (const status of [400, 403, 404, 405, 410, 422, 501]) {
      expect(retriesOn(new ApiError(status, 'no'))).toBe(false)
    }
  })

  it('retries a throttle, since that one clears on its own', () => {
    expect(retriesOn(new ApiError(429, 'too many requests'))).toBe(true)
  })

  it('waits as long as a throttle asked instead of guessing a backoff', () => {
    // A rate limiter in front of the hub knows its own window. The default
    // backoff for the first attempt is 1.5–4.5s; coming back then, when the
    // server said 30s, earns another 429 and never converges.
    const delays: number[] = []
    const spy = vi.spyOn(globalThis, 'setTimeout').mockImplementation(((
      _fn: () => void,
      ms?: number,
    ) => {
      delays.push(ms ?? 0)
      return 0 as unknown as ReturnType<typeof setTimeout>
    }) as typeof setTimeout)

    fire(new ApiError(429, 'slow down', 30_000), 0)
    spy.mockRestore()

    expect(delays).toHaveLength(1)
    // At least the window it named, and not wildly past it.
    expect(delays[0]).toBeGreaterThanOrEqual(30_000)
    expect(delays[0]).toBeLessThan(32_000)
  })

  it('caps an implausible Retry-After rather than parking the page for an hour', () => {
    const delays: number[] = []
    const spy = vi.spyOn(globalThis, 'setTimeout').mockImplementation(((
      _fn: () => void,
      ms?: number,
    ) => {
      delays.push(ms ?? 0)
      return 0 as unknown as ReturnType<typeof setTimeout>
    }) as typeof setTimeout)

    fire(new ApiError(503, 'maintenance', 3_600_000), 0)
    spy.mockRestore()

    expect(delays[0]).toBeLessThanOrEqual(61_000)
  })

  it('retries a server error, which may be transient', () => {
    // The case the retry exists for: the server restarted mid-poll.
    expect(retriesOn(new ApiError(503, 'unavailable'))).toBe(true)
  })

  it('retries a network failure with no status at all', () => {
    expect(retriesOn(new TypeError('Failed to fetch'))).toBe(true)
  })

  it('stops once the retry budget is spent', () => {
    const limit = SWR_OPTIONS.errorRetryCount ?? 3
    expect(retriesOn(new ApiError(503, 'unavailable'), limit - 1)).toBe(true)
    expect(retriesOn(new ApiError(503, 'unavailable'), limit)).toBe(false)
  })

  it('backs off further on each attempt, and jitters', () => {
    // Deterministic "random" so the growth is checkable; the jitter itself is
    // asserted separately below.
    const delays: number[] = []
    const spy = vi.spyOn(globalThis, 'setTimeout').mockImplementation(((
      _fn: () => void,
      ms?: number,
    ) => {
      delays.push(ms ?? 0)
      return 0 as unknown as ReturnType<typeof setTimeout>
    }) as typeof setTimeout)
    vi.spyOn(Math, 'random').mockReturnValue(0.5)

    for (const retryCount of [0, 1, 2]) {
      fire(new ApiError(503, 'unavailable'), retryCount)
    }
    spy.mockRestore()

    expect(delays).toHaveLength(3)
    // Strictly increasing: a server that is still down must be asked less
    // often, not at a fixed cadence.
    expect(delays[1]).toBeGreaterThan(delays[0])
    expect(delays[2]).toBeGreaterThan(delays[1])
  })

  it('spreads retries across tabs rather than synchronising them', () => {
    // Two clients failing at the same instant must not come back at the same
    // instant — that is the thundering herd the jitter is for.
    const seen = new Set<number>()
    const spy = vi.spyOn(globalThis, 'setTimeout').mockImplementation(((
      _fn: () => void,
      ms?: number,
    ) => {
      seen.add(ms ?? 0)
      return 0 as unknown as ReturnType<typeof setTimeout>
    }) as typeof setTimeout)

    for (let i = 0; i < 20; i++) fire(new ApiError(503, 'unavailable'), 0)
    spy.mockRestore()

    expect(seen.size).toBeGreaterThan(1)
  })
})
