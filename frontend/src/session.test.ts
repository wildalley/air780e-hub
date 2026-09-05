/**
 * The four phases, and the two precedence rules that are the reason they exist:
 * a session already known to be over beats a cached "authenticated", and a
 * status already in hand beats a failed revalidation.
 */
import { describe, expect, it } from 'vitest'
import { ApiError } from './api'
import { sessionState } from './session'

const signedIn = { configured: true, authenticated: true }
const signedOut = { configured: true, authenticated: false }

describe('sessionState', () => {
  it('waits instead of guessing before the first answer', () => {
    // Not `anonymous`: rendering the login form here would flash it at an
    // operator who is in fact logged in.
    expect(sessionState(undefined, undefined, null)).toEqual({
      phase: 'initializing',
      needsSetup: false,
      notice: null,
    })
  })

  it('opens the app when the server says the cookie is good', () => {
    expect(sessionState(signedIn, undefined, null).phase).toBe('authenticated')
  })

  it('asks for the password when there is no session', () => {
    expect(sessionState(signedOut, undefined, null)).toEqual({
      phase: 'anonymous',
      needsSetup: false,
      notice: null,
    })
  })

  it('turns the login form into first-run setup when no password is set', () => {
    const state = sessionState({ configured: false, authenticated: false }, undefined, null)
    expect(state.phase).toBe('anonymous')
    expect(state.needsSetup).toBe(true)
  })

  it('does not call a dead network a wrong password', () => {
    const state = sessionState(undefined, new TypeError('Failed to fetch'), null)
    expect(state.phase).toBe('unavailable')
    expect(state.notice).toBe('无法连接服务器,请检查网络或服务状态')
  })

  it('sends a 401 on the status read to the login form, not the error panel', () => {
    // `/auth/status` answers 200 for both cases today, so this is the defensive
    // half: if it ever starts 401-ing, that means "log in", not "we are broken".
    const state = sessionState(undefined, new ApiError(401, 'Unauthorized'), null)
    expect(state.phase).toBe('anonymous')
    expect(state.notice).toBeNull()
  })

  it('keeps a working session through a failed revalidation', () => {
    // Losing the network for five seconds must not close the transcript the
    // operator is reading.
    expect(sessionState(signedIn, new TypeError('Failed to fetch'), null).phase)
      .toBe('authenticated')
  })

  it('believes a 401 from anywhere over a cached authenticated status', () => {
    // The 401 came from the server itself; the cached status is just old.
    const state = sessionState(signedIn, undefined, 'lapsed')
    expect(state.phase).toBe('anonymous')
    expect(state.notice).toBe('登录状态已失效,请重新登录。')
  })

  it('explains nothing to someone who pressed 退出', () => {
    const state = sessionState(signedIn, undefined, 'signed-out')
    expect(state.phase).toBe('anonymous')
    expect(state.notice).toBeNull()
  })

  it('still offers setup if the session ended before a password was ever set', () => {
    // Reachable: first-run setup succeeded, the setup request's own follow-up
    // 401'd, and the status in cache is still the pre-setup one.
    const state = sessionState({ configured: false, authenticated: false }, undefined, 'lapsed')
    expect(state.needsSetup).toBe(true)
  })
})
