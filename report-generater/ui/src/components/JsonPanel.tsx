import { useMemo, useState } from 'react'
import { Copy, Check } from 'lucide-react'

type Props = {
  /** Omit to hide heading (e.g. when wrapped in CollapsibleSection) */
  title?: string
  value: unknown
  emptyHint?: string
}

export function JsonPanel({ title, value, emptyHint }: Props) {
  const [copied, setCopied] = useState(false)
  const text = useMemo(() => {
    if (value === undefined || value === null) return ''
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }, [value])

  const copy = async () => {
    if (!text) return
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1600)
  }

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        {title ? (
          <h3 className="text-sm font-medium text-[var(--app-text)]">{title}</h3>
        ) : (
          <span />
        )}
        {text ? (
          <button
            type="button"
            onClick={() => void copy()}
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--app-border)] bg-[var(--app-surface)] px-2 py-1 text-[11px] text-[var(--app-muted)] transition hover:border-[color-mix(in_oklab,var(--app-accent)_45%,var(--app-border))] hover:text-[var(--app-text)]"
          >
            {copied ? (
              <Check className="size-3.5 text-emerald-500" />
            ) : (
              <Copy className="size-3.5" />
            )}
            {copied ? 'Copied' : 'Copy'}
          </button>
        ) : null}
      </div>
      {!text ? (
        <p className="text-xs text-[var(--app-muted)]">
          {emptyHint ?? 'Not loaded.'}
        </p>
      ) : (
        <pre className="max-h-[min(60vh,420px)] overflow-auto rounded-xl border border-[var(--app-border)] bg-[var(--app-code-bg)] p-3 text-[11px] leading-relaxed text-[var(--app-text-secondary)]">
          {text}
        </pre>
      )}
    </section>
  )
}
