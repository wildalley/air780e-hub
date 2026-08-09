import { detectOtp, hasOlderMessages, resolveThread, threadPreview } from '../messages'
import { describe, expect, it } from 'vitest'
import type { Conversation } from '../api'

/**
 * `detectOtp` drives the copy-code button on every received message, so its
 * edge cases are user-visible: a wrong match means the operator copies an
 * order number instead of the verification code they came for.
 */
describe('detectOtp', () => {
  it('finds a plain verification code', () => {
    expect(detectOtp('【某平台】验证码 123456,5分钟内有效')).toBe('123456')
  })

  it('returns the LAST run when several are present', () => {
    // Documented behaviour ("the last 4–8 digit run"), not an accident: the
    // code usually trails the boilerplate. Pinned so a future refactor to
    // first-match has to be a deliberate decision.
    expect(detectOtp('验证码 1234,订单号 567890')).toBe('567890')
  })

  it('ignores digit runs that are too long or too short', () => {
    // A phone number must not match a prefix of itself — that is what the
    // lookbehind/lookahead in OTP_RE exist for.
    expect(detectOtp('你的号码是 13800138000')).toBeNull()
    expect(detectOtp('123')).toBeNull()
    expect(detectOtp('123456789')).toBeNull()
  })

  it('does not match across a decimal point', () => {
    expect(detectOtp('余额 12.34 元')).toBeNull()
  })

  it('returns null when there is nothing to copy', () => {
    expect(detectOtp('没有数字')).toBeNull()
    expect(detectOtp('')).toBeNull()
  })

  it('is not affected by the global regex flag across calls', () => {
    // OTP_RE is module-level and /g: a stateful lastIndex would make the
    // second identical call return something different.
    expect(detectOtp('code: 9999')).toBe('9999')
    expect(detectOtp('code: 9999')).toBe('9999')
  })
})

/**
 * `resolveThread` replaced an effect that copied the clicked row into state and
 * then re-synced it whenever the list refetched. These cases are the reasons
 * that effect existed — they have to keep holding without it.
 */
describe('resolveThread', () => {
  const thread = (over: Partial<Conversation> = {}): Conversation => ({
    sim_id: 1,
    peer: '10086',
    device: 'ttyUSB0',
    last_id: 5,
    last_body: 'old',
    last_direction: 'in',
    last_status: 'received',
    last_ts: '2026-08-06T00:00:00',
    message_count: 2,
    sim_label: '移动主卡',
    ...over,
  })

  it('returns nothing when no thread is open', () => {
    expect(resolveThread(null, [thread()], null)).toBeNull()
  })

  it('prefers the list row over the one that was clicked', () => {
    // The point of the whole change: a reply lands, the list refetches, and the
    // header must show the new preview instead of the snapshot from the click.
    const clicked = thread({ last_id: 5, last_body: 'old', message_count: 2 })
    const refetched = thread({ last_id: 9, last_body: 'sent reply', message_count: 3 })

    const resolved = resolveThread({ peer: '10086', sim_id: 1 }, [refetched], clicked)

    expect(resolved?.last_body).toBe('sent reply')
    expect(resolved?.last_id).toBe(9)
    expect(resolved?.message_count).toBe(3)
  })

  it('matches on card as well as number', () => {
    // Same number on two cards is two conversations. Matching peer alone would
    // open the wrong one.
    const onCardOne = thread({ sim_id: 1, last_body: 'card one' })
    const onCardTwo = thread({ sim_id: 2, last_body: 'card two' })

    const resolved = resolveThread({ peer: '10086', sim_id: 2 }, [onCardOne, onCardTwo], null)

    expect(resolved?.last_body).toBe('card two')
  })

  it('treats a thread with no card as distinct from one on a card', () => {
    const unassigned = thread({ sim_id: null, last_body: 'no card' })
    const assigned = thread({ sim_id: 1, last_body: 'card one' })

    expect(resolveThread({ peer: '10086', sim_id: null }, [assigned, unassigned], null)?.last_body)
      .toBe('no card')
  })

  it('falls back to the clicked row when the list does not have it', () => {
    // Opened from full-text search: the match can be older than the
    // conversations window, and the thread still has to render.
    const fromSearch = thread({ peer: '95533', last_body: 'found by search' })

    const resolved = resolveThread({ peer: '95533', sim_id: 1 }, [thread()], fromSearch)

    expect(resolved?.last_body).toBe('found by search')
  })

  it('falls back while the list is still loading', () => {
    // `threads` is undefined on first paint. Returning null here would flash
    // the empty state over a thread the user just clicked.
    const clicked = thread()
    expect(resolveThread({ peer: '10086', sim_id: 1 }, undefined, clicked)).toBe(clicked)
  })
})

/**
 * `hasOlderMessages` decides whether the thread view offers "加载更早的消息".
 *
 * The thread grows its window instead of paging, so this is the only thing
 * standing between the operator and a button that either never appears on a
 * long history or never goes away on a short one.
 */
describe('hasOlderMessages', () => {
  it('offers more when the window is full and the thread is longer', () => {
    expect(hasOlderMessages(640, 200)).toBe(true)
  })

  it('stops offering once every message has been fetched', () => {
    // The exact-fit case: a thread of precisely one window. Comparing the total
    // against the *requested* limit instead of the fetched count would leave
    // the button up with nothing behind it.
    expect(hasOlderMessages(200, 200)).toBe(false)
    expect(hasOlderMessages(37, 37)).toBe(false)
  })

  it('says no for an empty thread', () => {
    expect(hasOlderMessages(0, 0)).toBe(false)
  })

  it('does not offer more when the count somehow trails the rows', () => {
    // A total that lags the page (a delete landing between the two queries)
    // must not render a button that would fetch nothing.
    expect(hasOlderMessages(5, 10)).toBe(false)
  })
})

/**
 * `threadPreview` is what the conversation list shows for the newest message.
 *
 * A data SMS has no text worth previewing: its decoded body is either mojibake
 * or empty, which is exactly what the list used to render.
 */
describe('threadPreview', () => {
  it('names a data SMS instead of previewing its bytes', () => {
    // The real case: a giffgaff OTA message whose body decoded to CJK garbage.
    expect(
      threadPreview({ last_body: '鼠S耸盘涌羹', last_is_binary: 1 }),
    ).toBe('运营商数据短信')
    // And the one that decoded to nothing at all.
    expect(threadPreview({ last_body: '', last_is_binary: 1 })).toBe('运营商数据短信')
  })

  it('previews real text unchanged', () => {
    expect(threadPreview({ last_body: '验证码 123456' })).toBe('验证码 123456')
    expect(threadPreview({ last_body: '验证码 123456', last_is_binary: 0 })).toBe(
      '验证码 123456',
    )
  })

  it('falls back for an empty text message', () => {
    // Not binary, genuinely empty — keep the old placeholder rather than
    // claiming it was data.
    expect(threadPreview({ last_body: '' })).toBe('(空)')
  })
})
