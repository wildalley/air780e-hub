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

describe('paged list endpoints', () => {
  const empty = { items: [], total: 0 }

  it('forwards the paging query to every log endpoint', async () => {
    // One shape across all five is what lets a single pager component drive
    // them; a call that dropped its query would silently serve page 1 forever.
    const cases: [string, () => Promise<unknown>][] = [
      ['/api/logs?limit=25&offset=50', () => api.logs('limit=25&offset=50')],
      ['/api/notify-logs?limit=25&offset=50', () => api.notifyLogs('limit=25&offset=50')],
      ['/api/task-logs?limit=25&offset=50', () => api.tasks.logs('limit=25&offset=50')],
      [
        '/api/operations/audit?limit=25&offset=50',
        () => api.operations.audit('limit=25&offset=50'),
      ],
    ]
    for (const [expected, call] of cases) {
      const spy = mockFetch({ json: async () => empty })
      await call()
      expect(spy.mock.calls[0][0]).toBe(expected)
    }
  })

  it('keeps the incident status filter alongside the paging query', async () => {
    // The pager and the 未解决/全部 toggle are independent controls; losing
    // either from the URL makes the other appear broken.
    const spy = mockFetch({ json: async () => empty })
    await api.operations.incidents('all', 'limit=100&offset=200')
    expect(spy.mock.calls[0][0]).toBe(
      '/api/operations/incidents?status=all&limit=100&offset=200',
    )
  })

  it('returns items and total straight through', async () => {
    mockFetch({ json: async () => ({ items: [{ id: 1 }], total: 4231 }) })
    const page = await api.logs('limit=1&offset=0')
    expect(page.items).toHaveLength(1)
    // The total is what the pager needs; the page length would cap it at 1.
    expect(page.total).toBe(4231)
  })
})

describe('message content filters', () => {
  it('forwards data filters to list and conversations', async () => {
    const spy = mockFetch({ json: async () => ({ items: [], total: 0 }) })
    await api.messages.list({ content: 'data' })
    expect(spy.mock.calls[0][0]).toBe('/api/messages?content=data')

    spy.mockClear()
    await api.messages.conversations('text')
    expect(spy.mock.calls[0][0]).toBe('/api/conversations?content=text')
  })
})
