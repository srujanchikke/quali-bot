import { useContext } from 'react'
import { ThemeContext, type ThemeCtx } from '@/theme/theme-context'

export function useTheme(): ThemeCtx {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within ThemeProvider')
  return ctx
}
