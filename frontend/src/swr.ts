/**
 * Shared SWR policy.
 *
 * Pages call `useSWR(key, fetcher)` and get caching, dedup, focus
 * revalidation and retry from here rather than each rebuilding a
 * `useEffect` + `setInterval` + `alive` flag by hand.
 *
 * Keys are the API paths (`/api/devices`), so a mutation anywhere can
 * revalidate a page it does not import by calling `mutate('/api/devices')`.
 */
import { useEffect, useState } from 'react'
import type { SWRConfiguration } from 'swr'
import { ApiError } from './api'

/** Polling cadence for the live views (dashboard, ops, nav badge). */
export const LIVE_MS = 15_000

/**
 * A value that settles `ms` after the last change.
 *
 * For search-as-you-type: pass the debounced value into the SWR key so a
 * request goes out per pause, not per keystroke.
 */
export function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])
  return settled
}

/** Statuses where a retry cannot succeed — the answer will not change. */
const TERMINAL = new Set([400, 401, 403, 404, 422])

export const SWR_OPTIONS: SWRConfiguration = {
  // Two mounts of the same key within this window share one request. Covers
  // the common case of a page and the nav badge wanting the same list.
  dedupingInterval: 4_000,
  errorRetryCount: 3,
  errorRetryInterval: 3_000,

  onErrorRetry: (error, _key, config, revalidate, options) => {
    // A lapsed session or a missing row will answer the same way forever;
    // retrying only adds load and, for 401, noise in the audit log.
    if (error instanceof ApiError && TERMINAL.has(error.status)) return
    const count = options.retryCount ?? 0
    if (count >= (config.errorRetryCount ?? 3)) return
    // Exponential backoff with jitter, so a server restart does not get a
    // synchronised thundering herd from every open tab.
    const base = config.errorRetryInterval ?? 3_000
    const delay = base * 2 ** count * (0.5 + Math.random())
    setTimeout(() => void revalidate({ retryCount: count + 1 }), delay)
  },
}
