/**
 * Pure logic behind the messages view.
 *
 * Separate from `pages/Messages.tsx` so the page module exports only its
 * component, and so these stay directly testable — each one has edge cases
 * that are user-visible if they regress.
 */
import type { Conversation, Message, ThreadScope } from './api'

/**
 * The last 4–8 digit run in the body, if any.  This is the whole point of the
 * box — the verification code.  Lookbehind/lookahead keep a longer run like a
 * phone number from matching a prefix of itself.
 *
 * Exported because the bubble also highlights the same runs it detects; the two
 * must never drift apart. Safe to share despite the `g` flag — `matchAll`
 * works on an internal clone, so it never advances `lastIndex` here.
 */
export const OTP_RE = /(?<!\d)(\d{4,8})(?!\d)/g

export function detectOtp(body: string): string | null {
  let code: string | null = null
  for (const match of body.matchAll(OTP_RE)) code = match[1]
  return code
}

/**
 * The scope of one thread, named rather than left as a null.
 *
 * A card-less thread is `unassigned`; omitting the card would mean *every*
 * card, which is what used to fold another card's messages from the same number
 * into a card-less conversation. See `ThreadScope` in `./api`.
 */
export function threadScope(sim_id: number | null): ThreadScope {
  return sim_id === null ? 'unassigned' : sim_id
}

/**
 * Cursor pages merged into one transcript, oldest first.
 *
 * Pages are fetched newest-first — page 0 is the live tail, each further page
 * continues from the previous one's cursor. Two things make the merge
 * non-trivial, and both are why this is a function with tests rather than a
 * `flat()` at the call site:
 *
 * - Pages overlap. While the operator reads history the tail keeps growing, so
 *   a re-read of page 0 covers rows a later page already returned. The first
 *   copy wins — page 0 is the one being revalidated, so it holds the newest
 *   delivery status.
 * - Timestamps tie. Every segment of a multipart SMS carries one SCTS, so the
 *   id has to break the tie exactly as the server's `ts DESC, id DESC` does,
 *   or a bubble jumps between renders.
 */
export function mergeThreadPages(
  pages: readonly { items: Message[] }[] | undefined,
): Message[] {
  const byId = new Map<number, Message>()
  for (const page of pages ?? []) {
    for (const message of page.items) {
      if (!byId.has(message.id)) byId.set(message.id, message)
    }
  }
  return [...byId.values()].sort((a, b) => {
    const at = Date.parse(a.ts) - Date.parse(b.ts)
    return Number.isNaN(at) || at === 0 ? a.id - b.id : at
  })
}

/**
 * One line of preview for the conversation list.
 *
 * A data SMS has no text to preview, and its decoded body is mojibake or empty
 * — the list used to show a run of CJK garbage, or "(空)" for the ones that
 * decoded to nothing at all. Naming what it is beats showing either.
 *
 * A damaged message is `is_binary` too — its `body` is mojibake for the same
 * reason — but it was a person writing to a person, so the damaged branch has
 * to come first or the list calls a personal SMS operator data. The preview
 * shows the salvaged fragment behind a marker, never bare: a fragment that
 * looks like a whole message is how someone concludes there was no code in a
 * message whose code was in the octets that went missing.
 */
export function messagePreview(
  message: Pick<Message, 'body' | 'is_binary' | 'truncated' | 'recovered_body'>,
): string {
  if (message.truncated) {
    const salvaged = (message.recovered_body || '').trim()
    return salvaged ? `⚠️ 损坏片段：${salvaged}` : '⚠️ 短信损坏，正文未恢复'
  }
  if (message.is_binary) return '运营商数据短信'
  return message.body || '(空)'
}

export function threadPreview(
  thread: Pick<Conversation, 'last_body'> & {
    last_is_binary?: number
    last_truncated?: number
    last_recovered_body?: string | null
  },
): string {
  return messagePreview({
    body: thread.last_body,
    is_binary: thread.last_is_binary,
    truncated: thread.last_truncated,
    recovered_body: thread.last_recovered_body,
  })
}

/** Human-readable aggregate delivery state for an outbound bubble. */
export function deliveryStatusLabel(status: string): string | null {
  return {
    pending: '等待回执',
    partial: '部分送达',
    delivered: '已送达',
    failed: '投递失败',
  }[status] ?? null
}

/**
 * The open thread, resolved against the current list rather than copied into
 * state when it was clicked.
 *
 * The list is the source of truth: after a reply is sent the refetched row
 * carries the new preview, timestamp and count, and the header has to show
 * them. Holding a snapshot in state and syncing it from an effect is what this
 * replaces.
 *
 * `fallback` covers a thread that is not in the list — one opened from search
 * can be older than the conversations window, and it must still render.
 */
export function resolveThread(
  open: Pick<Conversation, 'peer' | 'sim_id'> | null,
  threads: Conversation[] | undefined,
  fallback: Conversation | null,
): Conversation | null {
  if (!open) return null
  const fresh = threads?.find((t) => t.peer === open.peer && t.sim_id === open.sim_id)
  return fresh ?? fallback
}

/**
 * The module a reply should go out through.
 *
 * The card decides it: a thread belongs to an ICCID, and that ICCID sits in
 * exactly one module.  The module *name* is only a fallback for a thread with
 * no card recorded, and only when one module answers to it — a name is unique
 * within an agent, not across the fleet, so with two hosts each owning a
 * `modem-1` the first match could be the wrong site's card.  Returning nothing
 * disables the composer, which is the honest outcome: the operator picks the
 * card explicitly instead of the UI sending from whichever one it found first.
 */
export function resolveThreadDevice<T extends { id: number; name: string; iccid?: string }>(
  devices: readonly T[],
  thread: Pick<Conversation, 'device' | 'sim_iccid'>,
): T | undefined {
  if (thread.sim_iccid) {
    const byCard = devices.find((device) => device.iccid === thread.sim_iccid)
    if (byCard) return byCard
  }
  const named = devices.filter((device) => device.name === thread.device)
  return named.length === 1 ? named[0] : undefined
}
