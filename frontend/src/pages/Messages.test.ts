import {
  deliveryStatusLabel,
  detectOtp,
  mergeThreadPages,
  messagePreview,
  resolveThread,
  resolveThreadDevice,
  threadPreview,
  threadScope,
} from '../messages'
import { describe, expect, it } from 'vitest'
import type { Conversation, Message } from '../api'

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
 * `threadScope` is the whole null-SIM fix in one line.
 *
 * A card-less thread and "every card" used to be spelled the same way, so
 * opening the former listed the latter — another card's messages from the same
 * number, and a read count that disagreed with them.
 */
describe('threadScope', () => {
  it('names the card-less thread instead of omitting the filter', () => {
    expect(threadScope(null)).toBe('unassigned')
  })

  it('passes a real card through as itself', () => {
    expect(threadScope(7)).toBe(7)
  })
})

/**
 * `mergeThreadPages` assembles the cursor pages of one transcript.
 *
 * The window it replaced re-read the whole conversation every five seconds and
 * hit a 422 wall past 2,000 rows. What has to hold now is that pages which
 * overlap (the tail grows while you read history) and messages that share a
 * timestamp (every segment of a multipart SMS carries one SCTS) still produce
 * each message exactly once, in the order the operator reads.
 */
describe('mergeThreadPages', () => {
  const at = (id: number, ts: string, over: Partial<Message> = {}): Message => ({
    id,
    agent_id: 'home-arch',
    device: 'modem-1',
    sim_id: 1,
    direction: 'in',
    peer: '10086',
    body: `msg-${id}`,
    ts,
    status: 'received',
    segments: 1,
    error: null,
    ...over,
  })

  it('reads oldest first, newest last', () => {
    // The API answers newest first; a conversation is read the other way round.
    const merged = mergeThreadPages([
      { items: [at(3, '2026-09-05T12:00:00+00:00'), at(2, '2026-09-05T11:00:00+00:00')] },
      { items: [at(1, '2026-09-05T10:00:00+00:00')] },
    ])
    expect(merged.map((m) => m.id)).toEqual([1, 2, 3])
  })

  it('keeps one copy of a message two pages both returned', () => {
    // While the operator reads history the tail keeps growing, so a re-read of
    // page 0 covers rows the next page already carried. Rendering both would
    // duplicate a bubble — and duplicate its verification code.
    const merged = mergeThreadPages([
      { items: [at(9, '2026-09-05T12:00:00+00:00'), at(8, '2026-09-05T11:00:00+00:00')] },
      { items: [at(8, '2026-09-05T11:00:00+00:00'), at(7, '2026-09-05T10:00:00+00:00')] },
    ])
    expect(merged.map((m) => m.id)).toEqual([7, 8, 9])
  })

  it('prefers the live tail copy of a message it also has from history', () => {
    // Page 0 is the one being revalidated, so it holds the settled delivery
    // report; the cached older page still says pending.
    const merged = mergeThreadPages([
      { items: [at(8, '2026-09-05T11:00:00+00:00', { status: 'delivered' })] },
      { items: [at(8, '2026-09-05T11:00:00+00:00', { status: 'pending' })] },
    ])
    expect(merged).toHaveLength(1)
    expect(merged[0].status).toBe('delivered')
  })

  it('breaks a shared timestamp by id, as the server does', () => {
    // Every segment of a multipart SMS carries one SCTS. Without the tie-break
    // the bubbles reorder between renders.
    const stamp = '2026-09-05T12:00:00+00:00'
    const merged = mergeThreadPages([{ items: [at(12, stamp), at(10, stamp), at(11, stamp)] }])
    expect(merged.map((m) => m.id)).toEqual([10, 11, 12])
  })

  it('survives a timestamp it cannot parse', () => {
    // A row written by an older agent, or a clock that produced nonsense: order
    // by id rather than dropping the message out of the transcript.
    const merged = mergeThreadPages([{ items: [at(2, 'not a date'), at(1, 'not a date')] }])
    expect(merged.map((m) => m.id)).toEqual([1, 2])
  })

  it('has nothing to merge before the first page arrives', () => {
    expect(mergeThreadPages(undefined)).toEqual([])
    expect(mergeThreadPages([])).toEqual([])
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

  it('shows the salvage for a damaged message, not the data label', () => {
    // A damaged frame is `last_is_binary` too, and the old order called this
    // "运营商数据短信" — a person's SMS filed as operator traffic.
    expect(
      threadPreview({
        last_body: '鼠S耸盘涌羹',
        last_is_binary: 1,
        last_truncated: 1,
        last_recovered_body: 'code is 123456 for GitHub',
      }),
    ).toBe('⚠️ 损坏片段：code is 123456 for GitHub')
  })

  it('still says damaged when nothing was recovered', () => {
    // Silence here is the failure mode: the reader has to know a message
    // arrived at all before they can go dig out the PDU.
    expect(
      threadPreview({ last_body: '鼠S耸盘涌羹', last_is_binary: 1, last_truncated: 1 }),
    ).toBe('⚠️ 短信损坏，正文未恢复')
    expect(
      threadPreview({
        last_body: '鼠S耸盘涌羹',
        last_is_binary: 1,
        last_truncated: 1,
        last_recovered_body: '   ',
      }),
    ).toBe('⚠️ 短信损坏，正文未恢复')
  })
})

describe('messagePreview', () => {
  it('hides decoded bytes everywhere a data message is summarized', () => {
    expect(messagePreview({ body: '鼠S耸盘涌羹', is_binary: 1 })).toBe(
      '运营商数据短信',
    )
  })

  it('keeps text and the empty-message fallback', () => {
    expect(messagePreview({ body: '验证码 123456', is_binary: 0 })).toBe('验证码 123456')
    expect(messagePreview({ body: '' })).toBe('(空)')
  })

  it('marks a damaged message everywhere it is summarized', () => {
    // Dashboard's recent-messages list calls this too, so the fix reaches it
    // for free — but only if the damaged branch stays ahead of `is_binary`.
    expect(
      messagePreview({
        body: '鼠S耸盘涌羹',
        is_binary: 1,
        truncated: 1,
        recovered_body: 'code is 123456',
      }),
    ).toBe('⚠️ 损坏片段：code is 123456')
  })
})

describe('deliveryStatusLabel', () => {
  it('labels every aggregate delivery state', () => {
    expect(deliveryStatusLabel('pending')).toBe('等待回执')
    expect(deliveryStatusLabel('partial')).toBe('部分送达')
    expect(deliveryStatusLabel('delivered')).toBe('已送达')
    expect(deliveryStatusLabel('failed')).toBe('投递失败')
  })

  it('does not invent a receipt for legacy sent messages', () => {
    expect(deliveryStatusLabel('sent')).toBeNull()
    expect(deliveryStatusLabel('received')).toBeNull()
  })
})

/**
 * Which module a reply goes out through.  Getting this wrong sends from the
 * wrong SIM — a different number, a different bill, and a reply the recipient
 * cannot tie to the conversation they started.
 */
describe('resolveThreadDevice', () => {
  const modules = [
    { id: 1, name: 'modem-1', iccid: '8986000000000000001' },
    { id: 2, name: 'modem-1', iccid: '8986000000000000002' },
    { id: 3, name: 'modem-2', iccid: '8986000000000000003' },
  ]

  it('follows the card, not the module name', () => {
    const picked = resolveThreadDevice(modules, {
      device: 'modem-1',
      sim_iccid: '8986000000000000002',
    })
    expect(picked?.id).toBe(2)
  })

  it('falls back to a module name only when one module answers to it', () => {
    expect(
      resolveThreadDevice(modules, { device: 'modem-2', sim_iccid: undefined })?.id,
    ).toBe(3)
  })

  it('refuses to guess between two agents that share a module name', () => {
    // Two hosts can each call their first module modem-1.  Picking the first
    // match would reply from whichever one the list happened to return first.
    expect(
      resolveThreadDevice(modules, { device: 'modem-1', sim_iccid: undefined }),
    ).toBeUndefined()
  })

  it('is undefined when nothing matches, so the composer stays disabled', () => {
    expect(
      resolveThreadDevice(modules, { device: 'modem-9', sim_iccid: '8986000000000000009' }),
    ).toBeUndefined()
  })
})
