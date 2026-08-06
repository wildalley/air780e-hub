// Config comes from vitest/config, not vite: it is a superset that also types
// the `test` block, so the suite and the build cannot drift apart.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

// The server hosts the built bundle itself, so in dev we just proxy the API
// and the agent socket through to it.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'Air780E SMS Hub',
        short_name: 'SMS Hub',
        description: 'Air780E 多卡短信中枢',
        theme_color: '#0d0d0d',
        background_color: '#0d0d0d',
        display: 'standalone',
        icons: [
          {
            src: '/icon.svg',
            sizes: '192x192 512x512',
            type: 'image/svg+xml',
            purpose: 'any maskable'
          }
        ]
      }
    })
  ],
  server: {
    port: 5173,
    // HUB_DEV_SERVER lets the proxy follow a server that is not on the default
    // port — useful when something else already holds 8080 on the dev machine.
    proxy: (() => {
      const target = process.env.HUB_DEV_SERVER ?? '127.0.0.1:8080'
      return {
        '/api': { target: `http://${target}`, changeOrigin: true },
        '/ws': { target: `ws://${target}`, ws: true },
        '/healthz': { target: `http://${target}`, changeOrigin: true },
      }
    })(),
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    // The suite tests app code only; build output and the generated service
    // worker are not source.
    include: ['src/**/*.test.{ts,tsx}'],
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        // Split the three heavy dependencies out of the app chunk so a UI
        // change does not invalidate ~800 kB of unchanged vendor code.
        manualChunks: {
          react: ['react', 'react-dom', 'react-router'],
          mui: ['@mui/material', '@mui/icons-material', '@emotion/react', '@emotion/styled'],
          charts: ['recharts'],
        },
      },
    },
  },
})
