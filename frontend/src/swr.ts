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
import { useCallback, useEffect, useState } from 'react'
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

/**
 * Offset paging state for a log view.
 *
 * Lives here with `useDebounced` because its product is the same thing: a
 * fragment of an SWR key. Putting `query` in the key caches each page
 * separately, so stepping back to one already seen is instant.
 *
 * The log endpoints used to return only their newest N rows, so anything older
 * was unreachable however long retention kept it.
 */
export function usePager(pageSize = 50) {
  const [page, setPage] = useState(0)
  const [size, setSize] = useState(pageSize)
  return {
    page,
    limit: size,
    offset: page * size,
    setPage,
    /** Changing page size returns to the first page rather than stranding you
     *  on an index the smaller list no longer has. */
    setSize: useCallback((next: number) => {
      setSize(next)
      setPage(0)
    }, []),
    reset: useCallback(() => setPage(0), []),
    query: `limit=${size}&offset=${page * size}`,
  }
}

/**
 * Statuses where a retry cannot succeed — the answer will not change.
 *
 * These are the app's own mistakes or a permanent refusal: a lapsed session, a
 * row that is gone, a body the endpoint will never accept, a route that does
 * not exist. Backing off and asking again just spends the operator's battery
 * and, for 401, fills the audit log.
 */
const TERMINAL = new Set([400, 401, 403, 404, 405, 410, 422, 501])

/** Never wait longer than this, whatever `Retry-After` claims. */
const RETRY_AFTER_CAP_MS = 60_000

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
    let delay = base * 2 ** count * (0.5 + Math.random())
    // A throttle told us its own window: obey it instead of guessing, and never
    // come back early — arriving before it reopens earns another 429.
    if (error instanceof ApiError && error.retryAfterMs !== undefined) {
      delay = Math.min(error.retryAfterMs, RETRY_AFTER_CAP_MS) + Math.random() * 1_000
    }
    setTimeout(() => void revalidate({ retryCount: count + 1 }), delay)
  },
}
