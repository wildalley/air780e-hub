/**
 * Pure logic behind the messages view.
 *
 * Separate from `pages/Messages.tsx` so the page module exports only its
 * component, and so these stay directly testable — each one has edge cases
 * that are user-visible if they regress.
 */
import type { Conversation, Message } from './api'

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
 * Whether a thread has messages older than the ones already fetched.
 *
 * Compares against how many actually came back, not against the requested
 * window: the two differ on the last window, and comparing to the request
 * would leave "加载更早的消息" showing forever at the top of a fully-read
 * conversation.
 */
export function hasOlderMessages(total: number, fetched: number): boolean {
  return total > fetched
}

/**
 * One line of preview for the conversation list.
 *
 * A data SMS has no text to preview, and its decoded body is mojibake or empty
 * — the list used to show a run of CJK garbage, or "(空)" for the ones that
 * decoded to nothing at all. Naming what it is beats showing either.
 */
export function messagePreview(
  message: Pick<Message, 'body' | 'is_binary'>,
): string {
  if (message.is_binary) return '运营商数据短信'
  return message.body || '(空)'
}

export function threadPreview(
  thread: Pick<Conversation, 'last_body'> & { last_is_binary?: number },
): string {
  return messagePreview({
    body: thread.last_body,
    is_binary: thread.last_is_binary,
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
