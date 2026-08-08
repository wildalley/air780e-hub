/**
 * `usePager` drives the log views.
 *
 * The properties that matter are the ones the old bare-`LIMIT` endpoints could
 * not express: an offset derived from the page, a query string stable enough to
 * use as an SWR key, and a size change that does not leave you stranded on a
 * page that no longer exists.
 */
import { act, render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { usePager } from './common'

type Pager = ReturnType<typeof usePager>

/** Renders the hook and hands back its latest value. */
function harness(pageSize?: number) {
  const state: { current: Pager } = { current: null as unknown as Pager }

  function Probe() {
    state.current = usePager(pageSize)
    return null
  }

  render(<Probe />)
  return state
}

describe('usePager', () => {
  it('starts on the first page with no offset', () => {
    const pager = harness()
    expect(pager.current.page).toBe(0)
    expect(pager.current.offset).toBe(0)
    expect(pager.current.limit).toBe(50)
  })

  it('derives the offset from the page and size', () => {
    const pager = harness(25)
    act(() => pager.current.setPage(3))
    expect(pager.current.offset).toBe(75)
    expect(pager.current.query).toBe('limit=25&offset=75')
  })

  it('returns to the first page when the size changes', () => {
    // Page 8 of 25 does not exist once the size becomes 200; keeping the index
    // would request offset 1600 and render a blank table.
    const pager = harness(25)
    act(() => pager.current.setPage(8))
    act(() => pager.current.setSize(200))
    expect(pager.current.page).toBe(0)
    expect(pager.current.offset).toBe(0)
    expect(pager.current.limit).toBe(200)
  })

  it('resets on demand, for when a filter narrows the list', () => {
    const pager = harness()
    act(() => pager.current.setPage(4))
    act(() => pager.current.reset())
    expect(pager.current.page).toBe(0)
  })

  it('produces a query string usable as a cache key', () => {
    // Two pagers on the same page must agree, so revisiting a page is a cache
    // hit rather than a refetch.
    const first = harness(50)
    const second = harness(50)
    act(() => first.current.setPage(2))
    act(() => second.current.setPage(2))
    expect(first.current.query).toBe(second.current.query)
    expect(first.current.query).toBe('limit=50&offset=100')
  })
})
