import { Loader2, RefreshCw } from 'lucide-react'
import type { Theme } from '@/theme/theme-context'

type Props = {
  theme: Theme
  runs: string[]
  loadingList: boolean
  loadingRun: boolean
  listError: string | null
  selectedId: string | null
  onSelect: (runId: string) => void
  onRefreshList: () => void
}

export function RunPickerPanel({
  theme,
  runs,
  loadingList,
  loadingRun,
  listError,
  selectedId,
  onSelect,
  onRefreshList,
}: Props) {
  const activeRow =
    theme === 'light'
      ? 'border-[color-mix(in_oklab,var(--app-accent)_45%,var(--app-border))] bg-[var(--app-accent-muted)] text-[var(--app-accent)]'
      : 'border-[color-mix(in_oklab,var(--app-accent)_45%,transparent)] bg-[var(--app-accent-muted)] text-sky-100'
  return (
    <div className="flex h-full min-h-[420px] flex-col rounded-2xl border border-dashed border-[var(--app-border)] bg-[var(--app-placeholder-bg)]">
      <div className="flex items-center justify-between gap-3 border-b border-[var(--app-border)] px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold text-[var(--app-text)]">
            Output runs
          </h3>
          <p className="text-xs text-[var(--app-muted)]">
            Folders under <span className="font-mono">report-generater/output</span>
          </p>
        </div>
        <button
          type="button"
          onClick={onRefreshList}
          disabled={loadingList}
          className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--app-border)] bg-[var(--app-surface)] px-2.5 py-1.5 text-xs font-medium text-[var(--app-text-secondary)] transition hover:border-[color-mix(in_oklab,var(--app-accent)_40%,var(--app-border))] disabled:opacity-50"
        >
          <RefreshCw
            className={`size-3.5 ${loadingList ? 'animate-spin' : ''}`}
            aria-hidden
          />
          Refresh
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {loadingList && runs.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-16 text-[var(--app-muted)]">
            <Loader2 className="size-8 animate-spin text-[var(--app-accent)]" />
            <p className="text-sm">Loading runs…</p>
          </div>
        ) : null}

        {listError ? (
          <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm">
            <p
              className={`font-medium ${theme === 'light' ? 'text-amber-900' : 'text-amber-200'}`}
            >
              Could not list output folder
            </p>
            <p className="mt-1 text-xs text-[var(--app-muted)]">{listError}</p>
            <p className="mt-2 text-xs text-[var(--app-muted)]">
              Listing runs requires the Vite dev server or{' '}
              <span className="font-mono">npm run preview</span> from{' '}
              <span className="font-mono">report-generater/ui</span> so it can read{' '}
              <span className="font-mono">report-generater/output</span> on disk.
            </p>
          </div>
        ) : null}

        {!loadingList && !listError && runs.length === 0 ? (
          <p className="py-12 text-center text-sm text-[var(--app-muted)]">
            No runs yet. Generate a report so a folder appears under{' '}
            <span className="font-mono text-[var(--app-text-secondary)]">
              report-generater/output/
            </span>
          </p>
        ) : null}

        <ul className="flex flex-col gap-2">
          {runs.map((id) => {
            const active = selectedId === id
            return (
              <li key={id}>
                <button
                  type="button"
                  disabled={loadingRun}
                  onClick={() => onSelect(id)}
                  className={`flex w-full items-center justify-between gap-3 rounded-xl border px-4 py-3 text-left text-sm transition ${
                    active
                      ? activeRow
                      : 'border-[var(--app-border)] bg-[var(--app-surface)] text-[var(--app-text)] hover:border-[color-mix(in_oklab,var(--app-accent)_35%,var(--app-border))]'
                  }`}
                >
                  <span className="font-mono text-xs sm:text-sm">{id}</span>
                  {loadingRun && active ? (
                    <Loader2 className="size-4 shrink-0 animate-spin" />
                  ) : (
                    <span className="text-[11px] text-[var(--app-muted)]">
                      {active ? 'Loaded' : 'Open'}
                    </span>
                  )}
                </button>
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}
