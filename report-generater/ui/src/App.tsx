import { useCallback, useEffect, useMemo, useState } from 'react'
import { CollapsibleSection } from '@/components/CollapsibleSection'
import { FlowBreadcrumb } from '@/components/FlowBreadcrumb'
import { FlowCanvas } from '@/components/FlowCanvas'
import { JsonPanel } from '@/components/JsonPanel'
import { CoverageReportHumanView } from '@/components/CoverageReportHumanView'
import { PathFlowCodeCoverage } from '@/components/PathFlowCodeCoverage'
import { RunPickerPanel } from '@/components/RunPickerPanel'
import {
  fetchOutputRunIds,
  loadRunFromOutputFolder,
} from '@/lib/fetchOutputRun'
import type { LoadedRun } from '@/types/reports'
import { useTheme } from '@/theme/useTheme'
import { GitBranch, LayoutDashboard, Moon, Sun } from 'lucide-react'

export default function App() {
  const { theme, toggle } = useTheme()
  const [run, setRun] = useState<LoadedRun | null>(null)
  const [reverse, setReverse] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [runs, setRuns] = useState<string[]>([])
  const [runsLoading, setRunsLoading] = useState(true)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)

  const refreshRuns = useCallback(async () => {
    setRunsLoading(true)
    setRunsError(null)
    try {
      const ids = await fetchOutputRunIds()
      setRuns(ids)
    } catch (e) {
      setRuns([])
      setRunsError(e instanceof Error ? e.message : 'Failed to list runs')
    } finally {
      setRunsLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshRuns()
  }, [refreshRuns])

  const selectOutputRun = useCallback(async (runId: string) => {
    setLoading(true)
    setLoadError(null)
    try {
      const r = await loadRunFromOutputFolder(runId)
      setActiveRunId(runId)
      setRun(r)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Failed to load run')
    } finally {
      setLoading(false)
    }
  }, [])

  const summary = useMemo(() => {
    if (!run?.finalReport) return null
    const fr = run.finalReport
    return {
      requestId: fr.request_id,
      endpoint: fr.api_call?.endpoint,
      status: fr.api_call?.http_status_code,
      error: fr.api_call?.error,
      leaf: fr.coverage_diff?.leaf,
      rootCause: fr.root_cause_analysis,
    }
  }, [run])

  const reverseOn =
    theme === 'light'
      ? 'border-[color-mix(in_oklab,var(--app-accent)_38%,var(--app-border))] bg-[var(--app-accent-muted)] text-[var(--app-accent)]'
      : 'border-[color-mix(in_oklab,var(--app-accent)_45%,transparent)] bg-[var(--app-accent-muted)] text-sky-100'
  const reverseOff =
    'border-[var(--app-border)] bg-[var(--app-surface)] text-[var(--app-text-secondary)] hover:border-[color-mix(in_oklab,var(--app-accent)_35%,var(--app-border))]'

  return (
    <div className="flex min-h-dvh flex-col bg-[var(--app-bg)] text-[var(--app-text)]">
      <header
        className="sticky top-0 z-20 border-b border-[var(--app-border)] backdrop-blur-md"
        style={{ background: 'var(--app-header-bg)' }}
      >
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-4 px-5 py-4">
          <div className="flex items-center gap-3">
            <div
              className="flex size-10 items-center justify-center rounded-xl bg-gradient-to-br from-[var(--app-accent)] to-[var(--app-accent-hover)] shadow-[0_0_24px_var(--app-accent-glow)]"
            >
              <LayoutDashboard className="size-5 text-white" aria-hidden />
            </div>
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-[var(--app-accent)]">
                Hyperswitch
              </p>
              <h1 className="text-lg font-semibold tracking-tight text-[var(--app-text)]">
                Coverage flow explorer
              </h1>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={toggle}
              className="inline-flex items-center gap-2 rounded-xl border border-[var(--app-border)] bg-[var(--app-surface)] px-3 py-2 text-sm text-[var(--app-text-secondary)] transition hover:border-[color-mix(in_oklab,var(--app-accent)_40%,var(--app-border))]"
              aria-label={
                theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'
              }
            >
              {theme === 'dark' ? (
                <Sun className="size-4 text-amber-400" aria-hidden />
              ) : (
                <Moon className="size-4 text-slate-600" aria-hidden />
              )}
              <span className="hidden sm:inline">
                {theme === 'dark' ? 'Light' : 'Dark'}
              </span>
            </button>
            {run ? (
              <button
                type="button"
                onClick={() => setReverse((r) => !r)}
                className={`inline-flex items-center gap-2 rounded-xl border px-3 py-2 text-sm transition ${
                  reverse ? reverseOn : reverseOff
                }`}
              >
                <GitBranch className="size-4" aria-hidden />
                {reverse ? 'View: leaf → HTTP' : 'View: HTTP → leaf'}
              </button>
            ) : null}
          </div>
        </div>
        {loadError ? (
          <p
            className={`border-t border-red-500/20 bg-red-500/10 px-5 py-2 text-center text-sm ${
              theme === 'light' ? 'text-red-700' : 'text-red-200'
            }`}
          >
            {loadError}
          </p>
        ) : null}
      </header>

      <main className="mx-auto flex w-full max-w-[1600px] flex-1 flex-col gap-8 px-5 py-6">
        <section className="flex flex-col gap-5">
          {run ? (
            <>
              <div className="min-w-0 space-y-0">
                <h2 className="text-lg font-semibold tracking-tight text-[var(--app-text)] sm:text-xl">
                  Architecture & flow
                </h2>
                <FlowBreadcrumb
                  runId={run.id}
                  onNavigateToRuns={() => setRun(null)}
                />
              </div>
              <div className="h-[520px] w-full min-h-[420px]">
                <FlowCanvas
                  pathFlow={run.pathFlow}
                  finalReport={run.finalReport}
                  reverse={reverse}
                />
              </div>
            </>
          ) : (
            <div className="h-[520px] w-full">
              <RunPickerPanel
                theme={theme}
                runs={runs}
                loadingList={runsLoading}
                loadingRun={loading}
                listError={runsError}
                selectedId={activeRunId}
                onSelect={(id) => void selectOutputRun(id)}
                onRefreshList={() => void refreshRuns()}
              />
            </div>
          )}
        </section>

        {run ? (
          <div className="flex flex-col gap-3">
            <CollapsibleSection title="Run" defaultOpen>
              <div className="min-w-0 space-y-3 text-sm">
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                    Run id
                  </p>
                  <p
                    className="mt-1 break-all font-mono text-xs text-[var(--app-text)]"
                    title={run.id}
                  >
                    {run.id}
                  </p>
                </div>
                {summary?.requestId ? (
                  <div className="min-w-0">
                    <p className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                      Request id
                    </p>
                    <p
                      className="mt-1 break-all font-mono text-xs text-[var(--app-text-secondary)] [overflow-wrap:anywhere]"
                      title={summary.requestId}
                    >
                      {summary.requestId}
                    </p>
                  </div>
                ) : null}
              </div>
            </CollapsibleSection>

            <CollapsibleSection title="Coverage" defaultOpen>
              <div className="space-y-10">
                <div>
                  <h3 className="mb-3 text-sm font-semibold tracking-tight text-[var(--app-text)]">
                    Functions on the path (source lines and hit counts)
                  </h3>
                  <PathFlowCodeCoverage run={run} />
                </div>
                <div className="border-t border-[var(--app-border)] pt-8">
                  <h3 className="mb-4 text-sm font-semibold tracking-tight text-[var(--app-text)]">
                    Coverage run report
                  </h3>
                  <CoverageReportHumanView report={run.coverageReport} />
                </div>
              </div>
            </CollapsibleSection>

            {run.finalReport ? (
              <CollapsibleSection title="Summary" defaultOpen>
                <div className="grid gap-4 md:grid-cols-2">
                  <div>
                    <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                      API
                    </h3>
                    <p className="mt-2 font-mono text-xs text-[var(--app-muted)]">
                      {run.finalReport.api_call?.method}{' '}
                      {run.finalReport.api_call?.endpoint}
                    </p>
                    <p className="mt-2 text-xs text-[var(--app-muted)]">
                      Status{' '}
                      <span className="font-mono text-[var(--app-text)]">
                        {run.finalReport.api_call?.http_status_code ?? '—'}
                      </span>
                    </p>
                  </div>
                  <div>
                    <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                      Root cause
                    </h3>
                    <p className="mt-2 text-sm leading-relaxed text-[var(--app-muted)]">
                      {run.finalReport.root_cause_analysis?.reason ??
                        'No analysis block.'}
                    </p>
                  </div>
                  <div className="md:col-span-2">
                    <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                      Coverage (leaf)
                    </h3>
                    <p className="mt-2 text-sm text-[var(--app-muted)]">
                      {run.finalReport.coverage_diff?.leaf?.file}:
                      {run.finalReport.coverage_diff?.leaf?.def_line} — ratio{' '}
                      <span className="font-mono">
                        {run.finalReport.coverage_diff?.line_coverage_ratio ??
                          '—'}
                      </span>
                    </p>
                  </div>
                </div>
              </CollapsibleSection>
            ) : null}

            <CollapsibleSection title="Final report">
              <JsonPanel
                value={run.finalReport}
                emptyHint="No final report in this run."
              />
            </CollapsibleSection>

            <CollapsibleSection title="Raw files">
              <JsonPanel
                value={run.rawFiles}
                emptyHint="No file contents loaded."
              />
            </CollapsibleSection>
          </div>
        ) : null}
      </main>
    </div>
  )
}
