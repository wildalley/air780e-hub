// Loaded before every test file (see `test.setupFiles` in vite.config.ts).
//
// jest-dom adds the DOM matchers (`toBeInTheDocument`, `toHaveAccessibleName`,
// …); the cleanup below unmounts anything a test rendered so state cannot leak
// into the next one.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// jsdom implements neither of these, and both are read at module scope by code
// under test: `matchMedia` by MUI's `useMediaQuery` (and by the theme's
// reduced-motion checks), `ResizeObserver` by recharts' responsive container.
// Without them the first render throws before any assertion runs.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}
