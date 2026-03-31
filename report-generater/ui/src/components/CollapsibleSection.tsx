import { useState, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'

type Props = {
  title: string
  defaultOpen?: boolean
  children: ReactNode
}

export function CollapsibleSection({
  title,
  defaultOpen = false,
  children,
}: Props) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className="overflow-hidden rounded-2xl border border-[var(--app-border)] bg-[var(--app-surface)]">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3.5 text-left transition hover:bg-[var(--app-elevated)]"
        aria-expanded={open}
      >
        <span className="text-sm font-semibold text-[var(--app-text)]">
          {title}
        </span>
        <ChevronDown
          className={`size-4 shrink-0 text-[var(--app-muted)] transition-transform duration-200 ${
            open ? 'rotate-180' : ''
          }`}
          aria-hidden
        />
      </button>
      {open ? (
        <div className="border-t border-[var(--app-border)] px-4 py-4">
          {children}
        </div>
      ) : null}
    </div>
  )
}
