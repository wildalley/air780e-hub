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
      // Warn, not error. All 16 current hits are the same shape: an async
      // `load()` whose setState runs *after* an await, not synchronously in the
      // effect body — the cascading-render case this rule targets. Moving them
      // to swr (already a dependency, used in Messages.tsx) is the real fix,
      // but that is an architectural change across 11 files and wants test
      // coverage first. Kept visible instead of switched off.
      'react-hooks/set-state-in-effect': 'warn',
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
