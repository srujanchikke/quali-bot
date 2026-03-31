import { createContext } from 'react'

export type Theme = 'dark' | 'light'

export type ThemeCtx = {
  theme: Theme
  setTheme: (t: Theme) => void
  toggle: () => void
}

export const ThemeContext = createContext<ThemeCtx | null>(null)

export const THEME_STORAGE_KEY = 'hs-flow-explorer-theme'
