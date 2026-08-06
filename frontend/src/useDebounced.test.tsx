/**
 * `useDebounced` feeds the search key in Messages.
 *
 * The property that matters: a term in flight must not reach the key until
 * typing stops, or the migration to swr would trade one request per keystroke
 * for one cache entry per keystroke — worse than the timer it replaced.
 */
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useDebounced } from './swr'

/** Renders the hook and records every settled value it produced. */
function harness(delay: number) {
  const seen: string[] = []

  function Probe({ value }: { value: string }) {
    const settled = useDebounced(value, delay)
    if (seen[seen.length - 1] !== settled) seen.push(settled)
    return null
  }

  const view = render(<Probe value="" />)
  return {
    seen,
    type: (value: string) => act(() => view.rerender(<Probe value={value} />)),
    wait: (ms: number) => act(() => void vi.advanceTimersByTime(ms)),
    unmount: view.unmount,
  }
}

describe('useDebounced', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('holds the value until typing stops', () => {
    const h = harness(250)
    h.type('1')
    h.type('12')
    h.type('123')
    // Mid-word: nothing has settled, so no request has been keyed.
    expect(h.seen).toEqual([''])

    h.wait(250)
    expect(h.seen).toEqual(['', '123'])
  })

  it('emits once per pause, not once per keystroke', () => {
    const h = harness(250)
    for (const value of ['a', 'ab', 'abc', 'abcd']) h.type(value)
    h.wait(250)
    h.type('abcde')
    h.wait(250)

    // Two pauses typed through five keystrokes.
    expect(h.seen).toEqual(['', 'abcd', 'abcde'])
  })

  it('restarts the wait on every change', () => {
    const h = harness(250)
    h.type('x')
    h.wait(200)
    h.type('xy')
    h.wait(200)
    // 400 ms elapsed but never 250 ms of quiet.
    expect(h.seen).toEqual([''])
    h.wait(50)
    expect(h.seen).toEqual(['', 'xy'])
  })

  it('does not settle after unmount', () => {
    const h = harness(250)
    h.type('gone')
    h.unmount()
    // A pending timer firing into an unmounted component would warn and, with
    // the key it feeds, kick off a request nobody is waiting for.
    expect(() => h.wait(250)).not.toThrow()
    expect(h.seen).toEqual([''])
  })
})
