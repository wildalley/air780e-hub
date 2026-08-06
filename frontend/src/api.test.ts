import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'

function mockFetch(response: Partial<Response> & { json?: () => Promise<unknown> }) {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => ({}),
    ...response,
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ApiError', () => {
  it('flags 401 as unauthenticated', () => {
    // App.tsx listens for this on unhandledrejection to bounce the whole UI
    // back to the login screen. Any other status must not trigger that.
    expect(new ApiError(401, 'nope').isUnauthenticated).toBe(true)
    expect(new ApiError(403, 'nope').isUnauthenticated).toBe(false)
    expect(new ApiError(500, 'boom').isUnauthenticated).toBe(false)
  })
})

describe('request error handling', () => {
  it("surfaces FastAPI's detail string", async () => {
    mockFetch({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      json: async () => ({ detail: '设备离线' }),
    })
    await expect(api.auth.status()).rejects.toThrow('设备离线')
  })

  it('unwraps the first entry of a validation detail array', async () => {
    mockFetch({
      ok: false,
      status: 422,
      statusText: 'Unprocessable',
      json: async () => ({ detail: [{ msg: 'field required' }] }),
    })
    await expect(api.auth.status()).rejects.toThrow('field required')
  })

  it('falls back to statusText when the body is not JSON', async () => {
    mockFetch({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: async () => {
        throw new Error('not json')
      },
    })
    await expect(api.auth.status()).rejects.toThrow('Bad Gateway')
  })

  it('carries the status code through as an ApiError', async () => {
    mockFetch({ ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}) })
    await expect(api.auth.status()).rejects.toBeInstanceOf(ApiError)
  })
})

describe('request success handling', () => {
  it('sends the session cookie', async () => {
    const spy = mockFetch({ json: async () => ({ configured: true, authenticated: true }) })
    await api.auth.status()
    expect(spy).toHaveBeenCalledWith('/api/auth/status', expect.objectContaining({
      credentials: 'same-origin',
    }))
  })

  it('sets a JSON content type only when there is a body', async () => {
    const spy = mockFetch({ json: async () => ({ ok: true }) })
    await api.auth.login('hunter2hunter')
    expect(spy.mock.calls[0][1]?.headers).toEqual({ 'Content-Type': 'application/json' })

    spy.mockClear()
    await api.auth.logout()
    expect(spy.mock.calls[0][1]?.headers).toBeUndefined()
  })

  it('returns undefined for 204 rather than parsing an empty body', async () => {
    mockFetch({
      status: 204,
      json: async () => {
        throw new Error('no body to parse')
      },
    })
    await expect(api.auth.logout()).resolves.toBeUndefined()
  })
})
