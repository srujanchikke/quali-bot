import { ChevronRight } from 'lucide-react'

type Props = {
  /** Current output run folder id, or `demo`, or null on the run picker */
  runId: string | null
  onNavigateToRuns: () => void
}

export function FlowBreadcrumb({ runId, onNavigateToRuns }: Props) {
  return (
    <nav aria-label="Breadcrumb" className="mt-3">
      <ol className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-[13px] leading-none">
        <li>
          {runId ? (
            <button
              type="button"
              onClick={onNavigateToRuns}
              className="rounded-md text-[var(--app-accent)] transition hover:underline"
            >
              Report runs
            </button>
          ) : (
            <span
              className="font-medium text-[var(--app-text-secondary)]"
              aria-current="page"
            >
              Report runs
            </span>
          )}
        </li>
        {runId ? (
          <>
            <li className="text-[var(--app-subtle)]" aria-hidden>
              <ChevronRight className="size-3.5" strokeWidth={2} />
            </li>
            <li>
              <span
                className="font-mono font-medium text-[var(--app-text-secondary)]"
                title={runId}
              >
                {runId}
              </span>
            </li>
          </>
        ) : null}
      </ol>
    </nav>
  )
}
