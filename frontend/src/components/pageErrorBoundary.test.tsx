import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { PageErrorBoundary } from './PageErrorBoundary'

/**
 * The last line under a page that throws while rendering.
 *
 * Without it React unmounts the whole tree on any render-time throw: the screen
 * goes white, the nav goes with it, and the only way out is a manual reload. The
 * two cases worth separating are a bug in a page (retry in place, the rest of
 * the app is fine) and a chunk that never downloaded, which is what a deploy
 * under an open tab looks like — that one needs a reload, and saying so beats
 * leaving the operator to guess.
 *
 * Fetch failures are not this component's job; they stay with the request layer
 * and `QueryState`.
 */

/** A child that throws on demand, the way a page with a bad render does. */
function Boom({ message }: { message: string }): React.ReactNode {
  throw new Error(message)
}

function mount(children: React.ReactNode, path = '/devices') {
  // React logs every caught error itself; the noise would bury a real failure.
  vi.spyOn(console, 'error').mockImplementation(() => {})
  return render(
    <MemoryRouter initialEntries={[path]}>
      <nav>
        <a href="/messages">短信</a>
      </nav>
      <PageErrorBoundary>{children}</PageErrorBoundary>
    </MemoryRouter>,
  )
}

describe('PageErrorBoundary', () => {
  it('lets a page that renders fine through untouched', () => {
    mount(<div>设备列表</div>)
    expect(screen.getByText('设备列表')).toBeInTheDocument()
  })

  it('explains a render failure and leaves the nav standing', () => {
    mount(<Boom message="Cannot read properties of undefined" />)
    expect(screen.getByText('页面渲染失败')).toBeInTheDocument()
    expect(screen.getByText(/Cannot read properties of undefined/)).toBeInTheDocument()
    // The whole point of catching here rather than at the root.
    expect(screen.getByRole('link', { name: '短信' })).toBeInTheDocument()
  })

  it('sends a missing chunk to a reload, because retrying in place cannot fix it', () => {
    // Vite's message when a lazy import 404s — the server shipped a new build
    // and the old hashed filenames are gone.
    mount(<Boom message="Failed to fetch dynamically imported module: /assets/Messages-a1b2.js" />)
    expect(screen.getByText('页面资源加载失败')).toBeInTheDocument()
    expect(screen.queryByText('页面渲染失败')).not.toBeInTheDocument()
  })

  it('retries in place, so a transient throw does not cost a reload', async () => {
    let fail = true
    function Flaky() {
      // Reading a module-scope flag on each render is the point: the retry
      // re-renders the same subtree, and this time it succeeds.
      if (fail) throw new Error('transient')
      return <div>设备列表</div>
    }
    mount(<Flaky />)
    expect(screen.getByText('页面渲染失败')).toBeInTheDocument()

    fail = false
    await userEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(screen.getByText('设备列表')).toBeInTheDocument()
  })

  it('clears itself on navigation, so one bad page does not poison the next', async () => {
    // Mirrors App.tsx: the boundary sits outside `Routes`, so the same instance
    // spans both pages and only its `key={pathname}` can reset the caught error.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <MemoryRouter initialEntries={['/devices']}>
        <Link to="/sims">去 SIM 卡</Link>
        <PageErrorBoundary>
          <Routes>
            <Route path="/devices" element={<Boom message="boom" />} />
            <Route path="/sims" element={<div>SIM 卡列表</div>} />
          </Routes>
        </PageErrorBoundary>
      </MemoryRouter>,
    )
    expect(screen.getByText('页面渲染失败')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('link', { name: '去 SIM 卡' }))
    expect(screen.getByText('SIM 卡列表')).toBeInTheDocument()
    expect(screen.queryByText('页面渲染失败')).not.toBeInTheDocument()
  })

  it('keys off the route, not the render, so state survives a re-render', () => {
    // A boundary keyed on anything that changes per render would throw its
    // caught error away immediately and loop.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { rerender } = render(
      <MemoryRouter initialEntries={['/devices']}>
        <Routes>
          <Route
            path="/devices"
            element={
              <PageErrorBoundary>
                <Boom message="boom" />
              </PageErrorBoundary>
            }
          />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('页面渲染失败')).toBeInTheDocument()
    rerender(
      <MemoryRouter initialEntries={['/devices']}>
        <Routes>
          <Route
            path="/devices"
            element={
              <PageErrorBoundary>
                <Boom message="boom" />
              </PageErrorBoundary>
            }
          />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('页面渲染失败')).toBeInTheDocument()
  })
})
