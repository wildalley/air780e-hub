import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, errorText, onSessionLapse } from './api'

function mockFetch(response: Partial<Response> & { json?: () => Promise<unknown> }) {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    statusText: 'OK',
    // A real Response always has these; the error path reads `Retry-After` off
    // them, so a stub without them fails on the throw rather than the assertion.
    headers: new Headers(),
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
    // `send` announces a lapse on this and nothing else, which is what returns
    // the whole UI to the login screen. Any other status must not trigger that.
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

  /**
   * `Retry-After` is what a throttle uses to say when it reopens. The retry
   * policy obeys it (see swr.test.ts); this is the half that reads it off the
   * wire, in both shapes RFC 9110 allows.
   */
  describe('Retry-After', () => {
    const failWith = (header?: string) =>
      mockFetch({
        ok: false,
        status: 429,
        statusText: 'Too Many Requests',
        headers: new Headers(header ? { 'Retry-After': header } : {}),
        json: async () => ({ detail: 'slow down' }),
      })

    const rejection = async (): Promise<ApiError> => {
      try {
        await api.devices.list()
        throw new Error('expected a rejection')
      } catch (error) {
        expect(error).toBeInstanceOf(ApiError)
        return error as ApiError
      }
    }

    it('reads delta-seconds', async () => {
      failWith('30')
      expect((await rejection()).retryAfterMs).toBe(30_000)
    })

    it('reads an HTTP-date', async () => {
      vi.useFakeTimers()
      vi.setSystemTime(new Date('2026-09-05T05:00:00Z'))
      try {
        failWith('Sat, 05 Sep 2026 05:00:45 GMT')
        expect((await rejection()).retryAfterMs).toBe(45_000)
      } finally {
        vi.useRealTimers()
      }
    })

    it('treats a date already past as "now", not as a negative wait', async () => {
      vi.useFakeTimers()
      vi.setSystemTime(new Date('2026-09-05T05:00:00Z'))
      try {
        failWith('Sat, 05 Sep 2026 04:59:00 GMT')
        expect((await rejection()).retryAfterMs).toBe(0)
      } finally {
        vi.useRealTimers()
      }
    })

    it('leaves it unset when the header is absent or unparseable', async () => {
      failWith()
      expect((await rejection()).retryAfterMs).toBeUndefined()
      failWith('soon please')
      expect((await rejection()).retryAfterMs).toBeUndefined()
    })
  })
})

describe('errorText', () => {
  it('prefers the server’s own words', () => {
    expect(errorText(new ApiError(400, '设备离线'))).toBe('设备离线')
  })

  it('names a request that never reached a server', () => {
    // `fetch` rejects with a TypeError for DNS failures, refused connections and
    // blocked requests alike. Reported as "请求失败" it read as a server answer.
    expect(errorText(new TypeError('Failed to fetch')))
      .toBe('无法连接服务器,请检查网络或服务状态')
  })

  it('does not dress a cancellation up as a failure', () => {
    expect(errorText(new DOMException('aborted', 'AbortError'))).toBe('请求已取消')
  })

  it('falls back for a status with no detail and for a non-error throw', () => {
    expect(errorText(new ApiError(500, ''), '读取失败')).toBe('读取失败')
    expect(errorText('boom', '读取失败')).toBe('读取失败')
  })
})

describe('session lapse announcements', () => {
  it('tells the app the session is gone before the caller sees the rejection', async () => {
    // The order matters: every write in the app catches its own failure, so a
    // handler that ran after the caller's `catch` would never bounce the UI.
    const seen: string[] = []
    const off = onSessionLapse(() => seen.push('lapse'))
    mockFetch({ ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}) })
    await api.messages.send(1, '10086', 'hi').catch(() => seen.push('caller'))
    expect(seen).toEqual(['lapse', 'caller'])
    off()
  })

  it('stays quiet when the wrong password is what got the 401', async () => {
    // Otherwise a failed login would report itself as an expired session, and the
    // form would tell the operator to log in again instead of "密码错误".
    const lapse = vi.fn()
    const off = onSessionLapse(lapse)
    mockFetch({ ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({ detail: '密码错误' }) })
    await expect(api.auth.login('nope')).rejects.toThrow('密码错误')
    expect(lapse).not.toHaveBeenCalled()
    off()
  })

  it('ignores the query string when deciding that, and still hears a 403', async () => {
    const lapse = vi.fn()
    const off = onSessionLapse(lapse)
    mockFetch({ ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}) })
    await expect(api.auth.status()).rejects.toThrow()
    expect(lapse).not.toHaveBeenCalled()
    // 403 is a different sentence — the session is real, the action is not
    // allowed — so it must not clear the screen.
    mockFetch({ ok: false, status: 403, statusText: 'Forbidden', json: async () => ({}) })
    await expect(api.messages.list({ scope: 'all' })).rejects.toThrow()
    expect(lapse).not.toHaveBeenCalled()
    off()
  })

  it('stops calling a handler that unsubscribed', async () => {
    const lapse = vi.fn()
    onSessionLapse(lapse)()
    mockFetch({ ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}) })
    await expect(api.messages.list({ scope: 'all' })).rejects.toThrow()
    expect(lapse).not.toHaveBeenCalled()
  })

  it('announces a lapse from a download too', async () => {
    // Downloads used to go through their own fetch wrapper, which parsed neither
    // the status nor the body — a 401 there left the page sitting there logged out.
    const lapse = vi.fn()
    const off = onSessionLapse(lapse)
    mockFetch({ ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}) })
    await expect(api.system.backup()).rejects.toThrow()
    expect(lapse).toHaveBeenCalledTimes(1)
    off()
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

/**
 * Which card a message read covers.
 *
 * "No card" and "every card" were both spelled as an absent `sim_id`, so opening
 * a card-less conversation listed another card's messages from the same number
 * while marking only some of them read. These assertions are the wire format of
 * that fix: three scopes, three distinct query strings, never two at once.
 */
describe('message query scope', () => {
  const page = { items: [], total: null, has_more: false, next_cursor: null }

  it('names the card-less thread instead of omitting the card', async () => {
    const spy = mockFetch({ json: async () => page })
    await api.messages.list({ scope: 'unassigned', peer: '10086' })
    expect(spy.mock.calls[0][0]).toBe('/api/messages?sim_scope=unassigned&peer=10086')
  })

  it('sends a real card as sim_id alone', async () => {
    // Both spellings at once is a 422 by design on the server; the client must
    // never be the one that produces it.
    const spy = mockFetch({ json: async () => page })
    await api.messages.list({ scope: 3, peer: '10086' })
    expect(spy.mock.calls[0][0]).toBe('/api/messages?sim_id=3&peer=10086')
  })

  it('omits the card filter only when every card was asked for', async () => {
    const spy = mockFetch({ json: async () => page })
    await api.messages.list({ scope: 'all', direction: 'in' })
    expect(spy.mock.calls[0][0]).toBe('/api/messages?direction=in')

    // The default, for the views that really do span the fleet.
    spy.mockClear()
    await api.messages.list({ direction: 'in' })
    expect(spy.mock.calls[0][0]).toBe('/api/messages?direction=in')
  })

  it('carries a cursor and drops the count for a live transcript', async () => {
    // The cursor is the server's own opaque token — a filter digest and a
    // (ts,id) position — and has to arrive byte-for-byte or it is refused.
    const cursor = 'MXwxYTJiM2M0ZHw5MTgyfDIwMjYtMDktMDVUMTA6MDA6MDA'
    const spy = mockFetch({ json: async () => page })
    await api.messages.list({
      scope: 1, peer: '10086', limit: 200, before: cursor, count: false,
    })
    expect(spy.mock.calls[0][0]).toBe(
      `/api/messages?sim_id=1&peer=10086&limit=200&before=${cursor}&count=false`,
    )
  })

  it('asks for the total by saying nothing, since that is the server default', async () => {
    const spy = mockFetch({ json: async () => page })
    await api.messages.list({ scope: 1, count: true })
    expect(spy.mock.calls[0][0]).toBe('/api/messages?sim_id=1')
  })

  it('exports exactly what the screen was filtered to', async () => {
    // A CSV that covered a different set than the view it was started from is
    // the reason the scope goes through one builder shared with `list`.
    Object.assign(URL, { createObjectURL: () => 'blob:messages', revokeObjectURL: () => {} })
    // jsdom cannot navigate, and the click is not what is under test here.
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const spy = mockFetch({
      blob: async () => new Blob(['id,body\n']),
      headers: new Headers(),
      json: async () => ({}),
    })
    await api.messages.exportCsv({ scope: 'unassigned', content: 'text' })
    expect(spy.mock.calls[0][0]).toBe(
      '/api/messages/export?sim_scope=unassigned&content=text',
    )
    expect(clicked).toHaveBeenCalled()
  })
})

/**
 * Marking read is a write, so its scope is narrower than a read's: one card or
 * no card, never "this number on every card", which would mark a number's
 * history read across the fleet.
 */
describe('marking a thread read', () => {
  it('spells the card-less thread both ways the endpoint accepts', async () => {
    const spy = mockFetch({ json: async () => ({ ok: true, marked: 0 }) })
    await api.messages.markRead('unassigned', '10086')
    expect(spy.mock.calls[0][0]).toBe('/api/messages/read')
    expect(JSON.parse(String(spy.mock.calls[0][1]?.body))).toEqual({
      sim_id: null, sim_scope: 'unassigned', peer: '10086',
    })
  })

  it('carries the watermark and the filter the transcript was read under', async () => {
    // `through_id` leaves a message that landed mid-request unread; `content`
    // keeps a text-only view from reading the data messages it was hiding.
    const spy = mockFetch({ json: async () => ({ ok: true, marked: 2 }) })
    await api.messages.markRead(3, '10086', 42, 'text')
    expect(JSON.parse(String(spy.mock.calls[0][1]?.body))).toEqual({
      sim_id: 3, peer: '10086', through_id: 42, content: 'text',
    })
  })

  it('has no way to spell "every card"', () => {
    // Compile-time, because there is no runtime guard to test: `markRead` takes
    // a ThreadScope, and 'all' is not one. If this ever type-checks, the type
    // stopped protecting a fleet-wide write.
    // @ts-expect-error 'all' is deliberately not assignable to ThreadScope
    expect(() => api.messages.markRead('all', '10086')).toBeTypeOf('function')
  })
})

describe('device network controls', () => {
  it('uses dedicated typed endpoints instead of the raw AT console', async () => {
    const spy = mockFetch({
      json: async () => ({ operators: [] }),
    })
    await api.devices.scanOperators(7)
    expect(spy.mock.calls[0][0]).toBe('/api/devices/by-id/7/operators/scan')
    expect(spy.mock.calls[0][1]?.method).toBe('POST')

    spy.mockClear()
    await api.devices.selectOperator(7, null)
    expect(spy.mock.calls[0][0]).toBe('/api/devices/by-id/7/operator')
    expect(JSON.parse(String(spy.mock.calls[0][1]?.body))).toEqual({ numeric: null })

    spy.mockClear()
    await api.devices.networkDiagnostics(7)
    expect(spy.mock.calls[0][0]).toBe('/api/devices/by-id/7/network-diagnostics')
  })

  /**
   * A module name is unique within one agent, not across the fleet.  Addressing
   * by row id is what keeps a command on the module the operator clicked, so
   * the client must not quietly fall back to a name-addressed path.
   */
  it('addresses commands and history by module row id', async () => {
    const spy = mockFetch({ json: async () => ({}) })
    await api.devices.refresh(3)
    expect(spy.mock.calls[0][0]).toBe('/api/devices/by-id/3/refresh')

    spy.mockClear()
    await api.devices.setRadio(3, false)
    expect(spy.mock.calls[0][0]).toBe('/api/devices/by-id/3/radio')

    spy.mockClear()
    await api.devices.history(3, 24)
    expect(spy.mock.calls[0][0]).toBe('/api/devices/by-id/3/history?hours=24')

    spy.mockClear()
    await api.messages.send(3, '10086', '余额')
    expect(JSON.parse(String(spy.mock.calls[0][1]?.body))).toEqual({
      device_id: 3, number: '10086', body: '余额',
    })

    spy.mockClear()
    await api.at(3, 'AT+CSQ')
    expect(JSON.parse(String(spy.mock.calls[0][1]?.body))).toEqual({
      device_id: 3, command: 'AT+CSQ',
    })
  })
})
