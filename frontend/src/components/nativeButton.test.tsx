import { render } from '@testing-library/react'
import { Link as RouterLink, MemoryRouter } from 'react-router'
import { Button, ListItemButton } from '@mui/material'
import { describe, expect, it, vi } from 'vitest'

/**
 * MUI v9 added a dev-only console.error when a ButtonBase-family component
 * receives a `component` that resolves to a non-<button> without an explicit
 * `nativeButton={false}` (useButtonBase.js). Router links are exempt:
 * ButtonBase sets `allowInferredHostMismatch` whenever `href` or `to` is
 * present, so `component={RouterLink} to="…"` is a supported shape.
 *
 * Pinned because the exemption is what lets these three call sites stay as
 * they are — Layout.tsx's nav items and Dashboard.tsx's two links. If a future
 * MUI drops it, this fails loudly instead of filling the console at runtime,
 * where neither typecheck nor the build would notice.
 */
describe('ButtonBase router-link contract', () => {
  it('Button with component={RouterLink} needs no nativeButton', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <MemoryRouter>
        <Button component={RouterLink} to="/messages" size="small">
          查看全部
        </Button>
      </MemoryRouter>,
    )
    expect(spy).not.toHaveBeenCalled()
  })

  it('ListItemButton with component={RouterLink} needs no nativeButton', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <MemoryRouter>
        <ListItemButton component={RouterLink} to="/devices" selected>
          设备
        </ListItemButton>
      </MemoryRouter>,
    )
    expect(spy).not.toHaveBeenCalled()
  })

  it('renders an anchor, not a button — the reason the exemption exists', () => {
    const { getByRole } = render(
      <MemoryRouter>
        <Button component={RouterLink} to="/messages">
          查看全部
        </Button>
      </MemoryRouter>,
    )
    const link = getByRole('link')
    expect(link.tagName).toBe('A')
    expect(link).toHaveAttribute('href', '/messages')
  })
})
