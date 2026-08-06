import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

// Flat config. Scoped to src/ — dist/ and the generated service worker are
// build output and must not be linted.
export default tseslint.config(
  { ignores: ['dist', 'dev-dist', 'coverage'] },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // tsc already reports unused locals/params via noUnusedLocals and
      // noUnusedParameters; a second identical error adds nothing.
      '@typescript-eslint/no-unused-vars': 'off',
      // Error, not warn: every data fetch now goes through swr (see src/swr.ts),
      // so a new `useEffect` that sets state from a fetch is a regression back
      // to hand-rolled caching, not a style preference. Selection defaults are
      // derived during render instead of seeded by an effect.
      'react-hooks/set-state-in-effect': 'error',
    },
  },
  {
    // Tests use jsdom globals and vitest's injected globals.
    files: ['**/*.test.{ts,tsx}', 'src/test-setup.ts'],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
  },
)
