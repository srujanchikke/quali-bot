import { useEffect, useState, type ReactNode } from 'react'
import {
  ThemeContext,
  type Theme,
  THEME_STORAGE_KEY,
} from '@/theme/theme-context'

function readInitialTheme(): Theme {
  if (typeof window === 'undefined') return 'dark'
  const s = localStorage.getItem(THEME_STORAGE_KEY) as Theme | null
  if (s === 'light' || s === 'dark') return s
  return window.matchMedia('(prefers-color-scheme: light)').matches
    ? 'light'
    : 'dark'
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readInitialTheme)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem(THEME_STORAGE_KEY, theme)
  }, [theme])

  const setTheme = (t: Theme) => setThemeState(t)
  const toggle = () =>
    setThemeState((x) => (x === 'dark' ? 'light' : 'dark'))

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggle }}>
      {children}
    </ThemeContext.Provider>
  )
}
