/**
 * What the app knows about the operator's session, as one value.
 *
 * The old rule was a single line — `statusError ? {authenticated: false} : fetched`
 * — which told anyone whose network had dropped that their password had stopped
 * working, and showed a blank screen until the first status answer arrived.
 * Four named phases keep those apart: nothing known yet, a session, no session,
 * and "we cannot ask".
 */
import { ApiError, errorText, type AuthStatus } from './api'

export type SessionPhase = 'initializing' | 'authenticated' | 'anonymous' | 'unavailable'

/** Why the business UI was taken down, when it was not the operator's doing. */
export type SessionEnd = 'lapsed' | 'signed-out'

export interface SessionState {
  phase: SessionPhase
  /** `anonymous` only: the login form doubles as first-run password setup. */
  needsSetup: boolean
  /** What brought them here, when it needs saying. */
  notice: string | null
}

/**
 * The session phase, from the status read and whatever ended the last session.
 *
 * Order matters:
 *
 * - `ended` wins over everything. A 401 from any request is the server's own
 *   word that the cookie is gone, and it must not be argued with by a cached
 *   `authenticated: true` sitting in the status cache.
 * - A status we have wins over an error, so a failed *revalidation* does not
 *   throw a working session out of the app. Losing the network for five seconds
 *   should not close the transcript the operator is reading.
 * - Only with no status at all does the error decide, and even then a 401 means
 *   "log in" while anything else means "we could not ask" — the distinction the
 *   whole type exists for.
 */
export function sessionState(
  status: AuthStatus | undefined,
  error: unknown,
  ended: SessionEnd | null,
): SessionState {
  const needsSetup = status ? !status.configured : false
  if (ended) {
    return {
      phase: 'anonymous',
      needsSetup,
      // A deliberate exit needs no explanation; a lapse does, or the login form
      // looks like it appeared for no reason.
      notice: ended === 'lapsed' ? '登录状态已失效,请重新登录。' : null,
    }
  }
  if (status) {
    return {
      phase: status.authenticated ? 'authenticated' : 'anonymous',
      needsSetup,
      notice: null,
    }
  }
  if (error !== undefined && error !== null) {
    if (error instanceof ApiError && error.isUnauthenticated) {
      return { phase: 'anonymous', needsSetup, notice: null }
    }
    return { phase: 'unavailable', needsSetup, notice: errorText(error, '无法连接服务器') }
  }
  return { phase: 'initializing', needsSetup, notice: null }
}
