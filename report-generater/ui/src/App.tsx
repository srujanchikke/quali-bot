import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import { CollapsibleSection } from '@/components/CollapsibleSection'
import { FlowBreadcrumb } from '@/components/FlowBreadcrumb'
import { FlowCanvas } from '@/components/FlowCanvas'
import { CoverageReportHumanView } from '@/components/CoverageReportHumanView'
import { PathFlowCodeCoverage } from '@/components/PathFlowCodeCoverage'
import { RunPickerPanel } from '@/components/RunPickerPanel'
import {
  fetchOutputRunIds,
  loadRunFromOutputFolder,
} from '@/lib/fetchOutputRun'
import { buildPathFlowCoverageBlocks } from '@/lib/pathFlowCoverage'
import type { LoadedRun } from '@/types/reports'
import { useTheme } from '@/theme/useTheme'
import { LayoutDashboard, Moon, Sun } from 'lucide-react'

type RunTab = 'overview' | 'testing' | 'coverage' | 'artifacts'

type AnsiStyleState = {
  fg?: string
  bold?: boolean
  dim?: boolean
  italic?: boolean
}

const ANSI_SGR_PATTERN = /\u001b\[([0-9;]*)m/g

const ANSI_COLORS: Record<number, string> = {
  30: '#94a3b8',
  31: '#f87171',
  32: '#4ade80',
  33: '#fbbf24',
  34: '#60a5fa',
  35: '#c084fc',
  36: '#22d3ee',
  37: '#e5e7eb',
  90: '#64748b',
  91: '#fca5a5',
  92: '#86efac',
  93: '#fcd34d',
  94: '#93c5fd',
  95: '#d8b4fe',
  96: '#67e8f9',
  97: '#f8fafc',
}

function parseJsonObject(text: string | undefined): unknown | undefined {
  if (!text) return undefined
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

function applyAnsiCode(state: AnsiStyleState, code: number): AnsiStyleState {
  if (code === 0) return {}
  if (code === 1) return { ...state, bold: true }
  if (code === 2) return { ...state, dim: true }
  if (code === 3) return { ...state, italic: true }
  if (code === 22) return { ...state, bold: false, dim: false }
  if (code === 23) return { ...state, italic: false }
  if (code === 39) return { ...state, fg: undefined }
  if (ANSI_COLORS[code]) return { ...state, fg: ANSI_COLORS[code] }
  return state
}

function ansiStyleToCss(style: AnsiStyleState) {
  return {
    color: style.fg,
    fontWeight: style.bold ? 700 : undefined,
    opacity: style.dim ? 0.72 : undefined,
    fontStyle: style.italic ? 'italic' : undefined,
  }
}

function renderAnsiText(text: string) {
  const lines = text.split('\n')

  return lines.map((line, lineIndex) => {
    const segments: Array<{ text: string; style: AnsiStyleState }> = []
    let style: AnsiStyleState = {}
    let cursor = 0

    for (const match of line.matchAll(ANSI_SGR_PATTERN)) {
      const matchIndex = match.index ?? 0
      if (matchIndex > cursor) {
        segments.push({
          text: line.slice(cursor, matchIndex),
          style,
        })
      }

      const codes = (match[1] || '0')
        .split(';')
        .map((part) => Number.parseInt(part || '0', 10))

      for (const code of codes) {
        style = applyAnsiCode(style, Number.isNaN(code) ? 0 : code)
      }

      cursor = matchIndex + match[0].length
    }

    if (cursor < line.length) {
      segments.push({
        text: line.slice(cursor),
        style,
      })
    }

    if (segments.length === 0) {
      segments.push({ text: '', style: {} })
    }

    return (
      <Fragment key={lineIndex}>
        {segments.map((segment, segmentIndex) => (
          <span
            key={`${lineIndex}-${segmentIndex}`}
            style={ansiStyleToCss(segment.style)}
          >
            {segment.text || (segmentIndex === 0 ? ' ' : '')}
          </span>
        ))}
        {lineIndex < lines.length - 1 ? '\n' : null}
      </Fragment>
    )
  })
}

function ArtifactFilePanel({
  text,
}: {
  text: string | undefined
}) {
  return (
    <section>
      {!text ? (
        <p className="text-xs text-[var(--app-muted)]">Not generated for this run.</p>
      ) : (
        <pre className="max-h-[min(72vh,640px)] overflow-auto whitespace-pre-wrap break-words rounded-xl border border-[var(--app-border)] bg-[#05080d] p-4 text-[12px] leading-relaxed text-[var(--app-text-secondary)] shadow-inner">
          {renderAnsiText(text)}
        </pre>
      )}
    </section>
  )
}

function statusTone(status: 'ok' | 'partial' | 'failed') {
  if (status === 'ok') {
    return 'border-emerald-500/20 bg-emerald-500/10 text-emerald-400'
  }
  if (status === 'failed') {
    return 'border-rose-500/20 bg-rose-500/10 text-rose-400'
  }
  return 'border-amber-500/20 bg-amber-500/10 text-amber-400'
}

function shortFileName(path: string | undefined) {
  if (!path) return undefined
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

function includesAny(text: string, needles: string[]) {
  const lower = text.toLowerCase()
  return needles.some((needle) => lower.includes(needle.toLowerCase()))
}

export default function App() {
  const { theme, toggle } = useTheme()
  const [run, setRun] = useState<LoadedRun | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [runs, setRuns] = useState<string[]>([])
  const [runsLoading, setRunsLoading] = useState(true)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<RunTab>('overview')

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
      setActiveTab('overview')
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

  const coverageHtmlUrl = useMemo(() => {
    if (!run?.id) return null
    return `/api/run-html/${encodeURIComponent(run.id)}/index.html`
  }, [run?.id])

  const cypressParsed = useMemo(
    () => parseJsonObject(run?.rawFiles['cypress_parsed.json']) as
      | {
          request_ids?: string[]
          passing_count?: number
          failing_count?: number
          total_tests?: number
          failed_test_names?: string[]
          errors?: string[]
          test_passed?: boolean
        }
      | undefined,
    [run],
  )

  const overviewCoverageBlock = useMemo(() => {
    if (!run) return null
    const blocks = buildPathFlowCoverageBlocks(
      run.pathFlow,
      run.coverageReport,
      run.finalReport,
      run.rawFiles['line_hits.txt'],
    )
    return blocks.find((block) => block.roleLabel === 'Leaf') ?? blocks[blocks.length - 1] ?? null
  }, [run])

  const overview = useMemo(() => {
    if (!run) return null

    const fallbackRequestIds = run.finalReport?.request_id
      ? [run.finalReport.request_id]
      : []
    const requestIds =
      cypressParsed?.request_ids?.length ? cypressParsed.request_ids : fallbackRequestIds
    const passingCount =
      cypressParsed?.passing_count ?? run.finalReport?.test_results?.passing_count ?? 0
    const failingCount =
      cypressParsed?.failing_count ??
      run.finalReport?.test_results?.failing_count ??
      run.finalReport?.api_call?.error?.failing_count ??
      0
    const totalTests =
      cypressParsed?.total_tests ?? run.finalReport?.test_results?.total_tests ?? 0
    const errors = [
      ...(cypressParsed?.errors ?? []),
      ...(run.finalReport?.api_call?.error?.errors ?? []),
      run.finalReport?.root_cause_analysis?.reason ?? '',
    ].filter(Boolean)
    const failedTests = [
      ...(cypressParsed?.failed_test_names ?? []),
      ...(run.finalReport?.api_call?.error?.failed_tests ?? []),
    ].filter(Boolean)
    const profrawAvailable = run.coverageReport?.d?.kind !== 'coverage_unavailable'
    const apiStatus = run.finalReport?.api_call?.http_status_code
    const apiError = run.finalReport?.api_call?.error
    const rca = run.finalReport?.root_cause_analysis
    const coverageRatio = run.finalReport?.coverage_diff?.line_coverage_ratio
    const leaf = run.finalReport?.coverage_diff?.leaf
    const routerLogAvailable =
      Boolean(run.rawFiles['router_run.log']) ||
      Boolean(run.finalReport?.router_log_correlation?.router_log_path) ||
      (run.finalReport?.router_log_correlation?.matches_in_log ?? 0) > 0

    const checkpoints: Array<{
      label: string
      status: 'ok' | 'partial' | 'failed'
      detail: string
    }> = [
      {
        label: 'Router',
        status: routerLogAvailable ? 'ok' : 'failed',
        detail: routerLogAvailable
          ? 'Router activity was captured for this run.'
          : 'Router log details were not found for this run.',
      },
      {
        label: 'Testing',
        status:
          totalTests === 0 ? 'failed' : failingCount > 0 ? 'partial' : 'ok',
        detail:
          totalTests === 0
            ? 'No Cypress tests were parsed from the run.'
            : `${passingCount} passed, ${failingCount} failed, ${Math.max(totalTests - passingCount - failingCount, 0)} remaining/pending.`,
      },
      {
        label: 'Coverage',
        status: profrawAvailable ? 'ok' : 'partial',
        detail: profrawAvailable
          ? 'LLVM profile data was collected and coverage artifacts were generated.'
          : 'Coverage artifacts are partial because no LLVM profile data was found.',
      },
      {
        label: 'Final report',
        status: run.finalReport ? 'ok' : 'failed',
        detail: run.finalReport
          ? 'Final RCA report was generated for this run.'
          : 'Final RCA report is missing.',
      },
    ]

    const coverageTarget =
      leaf?.name || shortFileName(leaf?.file) || undefined

    const routeMismatch =
      errors.some((error) => includesAny(error, ['/accounts', 'unrecognized request url'])) ||
      failedTests.some((test) => includesAny(test, ['account create']))
    const connectorSetupFailure =
      errors.some((error) => includesAny(error, ['connector create call failed'])) ||
      failedTests.some((test) => includesAny(test, ['connector account create']))
    const paymentFlowFailure =
      errors.some((error) =>
        includesAny(error, ['client_secret', 'expecting valid response']),
      ) ||
      failedTests.some((test) =>
        includesAny(test, ['create payment intent', 'confirm payment intent', 'capture payment intent']),
      )

    const whatWorked = [
      routerLogAvailable ? 'router activity was recorded' : null,
      run.rawFiles['flow_pipeline.log'] ? 'flow pipeline ran' : null,
      totalTests > 0 ? `Cypress executed ${totalTests} parsed checks` : null,
      requestIds.length ? 'request IDs were captured' : null,
      run.rawFiles['cypress_parsed.json'] ? 'Cypress results were parsed' : null,
      run.finalReport ? 'final report was generated' : null,
    ].filter(Boolean) as string[]

    const specBreakdown = [
      {
        spec: '00001-AccountCreate.cy.js',
        summary: 'merchant create and API key create are failing',
        show:
          failedTests.some((test) => includesAny(test, ['account create'])) ||
          errors.some((error) => includesAny(error, ['/accounts', 'api key create'])),
      },
      {
        spec: '00002-CustomerCreate.cy.js',
        summary: 'customer create is passing, so the environment is not universally broken',
        show: run.rawFiles['flow_pipeline.log']?.includes('00002-CustomerCreate.cy.js') ?? false,
      },
      {
        spec: '00003-ConnectorCreate.cy.js',
        summary: connectorSetupFailure
          ? 'connector create reaches the API, but the backend route/API still does not match what the test expects'
          : 'connector setup did not show a clear failure in this run',
        show:
          run.rawFiles['flow_pipeline.log']?.includes('00003-ConnectorCreate.cy.js') ?? false,
      },
      {
        spec: '00029-IncrementalAuth.cy.js',
        summary: paymentFlowFailure
          ? 'the target flow is reached, but payment create / confirm / capture still fail'
          : 'the target payment flow did not show a clear failure in this run',
        show:
          run.rawFiles['flow_pipeline.log']?.includes('00029-IncrementalAuth.cy.js') ?? false,
      },
    ].filter((item) => item.show)

    const narrative = [
      coverageRatio !== undefined
        ? `Current coverage for the focused area is ${coverageRatio}.${coverageTarget ? ` The target function is ${coverageTarget}.` : ''}`
        : profrawAvailable
          ? coverageTarget
            ? `Coverage artifacts were generated for this run. The target function is ${coverageTarget}.`
            : 'Coverage artifacts were generated for this run.'
          : 'Coverage is partial for this run because no LLVM profile files were collected.',
      routeMismatch
        ? 'The first visible blocker is the account setup flow: the backend is returning "Unrecognized request URL" for /accounts, so merchant and API key setup do not complete successfully.'
        : null,
      connectorSetupFailure
        ? 'Connector setup is also failing later in the run, which means the connector account is not being created in a usable state.'
        : null,
      paymentFlowFailure
        ? 'The payment flow reaches incremental authorization, but payment create / confirm / capture still fail because earlier setup and API responses are not in the expected state.'
        : null,
      !routeMismatch && !connectorSetupFailure && !paymentFlowFailure && rca?.reason
        ? rca.reason
        : null,
      totalTests
        ? `Overall, Cypress parsed ${totalTests} checks for this run with ${passingCount} passing and ${failingCount} failing.`
        : null,
      requestIds.length
        ? `The run captured ${requestIds.length} request id${requestIds.length === 1 ? '' : 's'}, so logs and failures can be correlated in the report.`
        : null,
    ].filter(Boolean) as string[]

    return {
      checkpoints,
      highlights: narrative,
      apiStatus,
      apiError,
      whatWorked,
      routeMismatch,
      connectorSetupFailure,
      paymentFlowFailure,
      requestIds,
      specBreakdown,
      profrawAvailable,
    }
  }, [run, cypressParsed])

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
                  reverse={false}
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
            <div className="inline-flex w-fit rounded-xl border border-[var(--app-border)] bg-[var(--app-surface)] p-1">
              {(
                [
                  ['overview', 'Overview'],
                  ['testing', 'Testing'],
                  ['coverage', 'Coverage'],
                  ['artifacts', 'Logs'],
                ] as [RunTab, string][]
              ).map(([tab, label]) => (
                <button
                  key={tab}
                  type="button"
                  onClick={() => setActiveTab(tab)}
                  className={`rounded-lg px-3 py-1.5 text-sm transition ${
                    activeTab === tab
                      ? 'bg-[var(--app-accent-muted)] text-[var(--app-accent)]'
                      : 'text-[var(--app-text-secondary)] hover:bg-[var(--app-elevated)]'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            {activeTab === 'overview' ? (
              <>
                {run.finalReport ? (
                  <CollapsibleSection title="Run summary" defaultOpen>
                    <div className="space-y-4">
                      <div className="grid gap-4 text-sm md:grid-cols-2">
                        <div className="min-w-0">
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

                      {overview ? (
                        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                          {overview.checkpoints.map((item) => (
                            <div
                              key={item.label}
                              className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-3"
                            >
                              <div className="flex items-start justify-between gap-2">
                                <p className="text-xs uppercase tracking-wider text-[var(--app-muted)]">
                                  {item.label}
                                </p>
                                <span
                                  className={`rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ${statusTone(item.status)}`}
                                >
                                  {item.status}
                                </span>
                              </div>
                              <p className="mt-2 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                                {item.detail}
                              </p>
                            </div>
                          ))}
                        </div>
                      ) : null}

                      {overview ? (
                        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
                          <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                            What happened overall
                          </h3>
                          <p className="mt-3 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                            The pipeline itself completed successfully:
                          </p>
                          <ul className="mt-3 list-disc space-y-1.5 pl-5 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                            {overview.whatWorked.map((item) => (
                              <li key={item}>{item}</li>
                            ))}
                          </ul>
                          <p className="mt-4 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                            So this is not a pipeline failure. It is a successful run of the
                            pipeline with failing business or test steps.
                          </p>
                        </div>
                      ) : null}

                      {overview?.highlights?.length ? (
                        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
                          <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                            Main things this run tells us
                          </h3>
                          <ol className="mt-3 space-y-3 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                            {overview.highlights.map((item, index) => (
                              <li key={item}>
                                <span className="font-semibold text-[var(--app-text)]">
                                  {index + 1}.
                                </span>{' '}
                                {item}
                              </li>
                            ))}
                          </ol>
                        </div>
                      ) : null}

                      {overview?.specBreakdown?.length ? (
                        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
                          <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                            Test breakdown
                          </h3>
                          <div className="mt-3 space-y-3">
                            {overview.specBreakdown.map((item) => (
                              <div
                                key={item.spec}
                                className="rounded-lg border border-[var(--app-border)] bg-[var(--app-surface)] p-3"
                              >
                                <p className="font-mono text-xs text-[var(--app-text)]">
                                  {item.spec}
                                </p>
                                <p className="mt-2 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                                  {item.summary}
                                </p>
                              </div>
                            ))}
                          </div>
                        </div>
                      ) : null}

                      <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
                        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                          <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                            Target function coverage
                          </h3>
                          {overviewCoverageBlock ? (
                            <span className="rounded-md bg-[var(--app-accent-muted)] px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--app-accent)]">
                              {overviewCoverageBlock.roleLabel}
                            </span>
                          ) : null}
                        </div>
                        {overviewCoverageBlock ? (
                          <>
                            <p className="mt-3 break-all font-mono text-xs text-[var(--app-text-secondary)]">
                              {overviewCoverageBlock.functionName}
                            </p>
                            <div className="mt-3 overflow-x-auto rounded-xl border border-[var(--app-border)] bg-[var(--app-code-bg)]">
                              <table className="w-full min-w-[min(100%,640px)] border-collapse text-left text-[11px] leading-snug">
                                <thead>
                                  <tr className="border-b border-[var(--app-border)] bg-[var(--app-elevated)] text-[var(--app-muted)]">
                                    <th className="w-12 px-2 py-1.5 font-medium">Line</th>
                                    <th className="w-14 px-2 py-1.5 font-medium">Hits</th>
                                    <th className="px-2 py-1.5 font-medium">Code</th>
                                  </tr>
                                </thead>
                                <tbody className="text-[var(--app-text)]">
                                  {overviewCoverageBlock.lines.map((row) => (
                                    <tr
                                      key={row.lineNumber}
                                      className={
                                        row.hits === null
                                          ? ''
                                          : row.hits === 0
                                            ? 'bg-red-500/[0.07] dark:bg-red-500/[0.09]'
                                            : 'bg-emerald-500/[0.06] dark:bg-emerald-500/[0.08]'
                                      }
                                    >
                                      <td className="whitespace-nowrap px-2 py-px align-top font-mono text-[var(--app-muted)]">
                                        {row.lineNumber}
                                      </td>
                                      <td className="whitespace-nowrap px-2 py-px align-top font-mono text-[var(--app-muted)]">
                                        {row.hits === null ? '—' : row.hits}
                                      </td>
                                      <td className="px-2 py-px align-top">
                                        <pre className="m-0 max-w-none whitespace-pre-wrap break-all font-mono text-[11px]">
                                          {row.text || ' '}
                                        </pre>
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                            <p className="mt-2 text-xs text-[var(--app-muted)]">
                              Gap status:{' '}
                              <span className="font-medium text-[var(--app-text)]">
                                {run.finalReport.coverage_diff?.gap_status?.replace(/_/g, ' ') ??
                                  '—'}
                              </span>
                            </p>
                          </>
                        ) : (
                          <p className="mt-3 text-xs text-[var(--app-muted)]">
                            {run.coverageReport?.d?.kind === 'coverage_unavailable'
                              ? `Coverage for the target function is unavailable for this run because ${String(run.coverageReport.d.error ?? 'no LLVM profile data was generated')}.`
                              : 'No embedded source is available for the target function in this run.'}
                          </p>
                        )}
                      </div>

                      <div className="grid gap-4 md:grid-cols-2">
                        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
                          <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                            API calls
                          </h3>
                          {run.pathFlow?.endpoints?.length ? (
                            <div className="mt-3 space-y-2">
                              {run.pathFlow.endpoints.map((endpoint) => (
                                <p
                                  key={`${endpoint.method}-${endpoint.path}-${endpoint.handler}`}
                                  className="break-all font-mono text-xs text-[var(--app-text-secondary)]"
                                >
                                  {endpoint.method} {endpoint.path}
                                </p>
                              ))}
                            </div>
                          ) : (
                            <p className="mt-3 break-all font-mono text-xs text-[var(--app-text-secondary)]">
                              {run.finalReport.api_call?.method}{' '}
                              {run.finalReport.api_call?.endpoint}
                            </p>
                          )}
                        </div>

                        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
                          <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                            Request id correlation
                          </h3>
                          <p className="mt-3 break-all font-mono text-xs text-[var(--app-text-secondary)]">
                            {overview?.requestIds?.join(', ') || 'No request id captured.'}
                          </p>
                        </div>
                      </div>
                    </div>
                  </CollapsibleSection>
                ) : (
                  <CollapsibleSection title="Run summary" defaultOpen>
                    <div className="grid gap-4 text-sm md:grid-cols-2">
                      <div className="min-w-0">
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
                )}
              </>
            ) : null}

            {activeTab === 'testing' ? (
              <CollapsibleSection title="Testing pipeline" defaultOpen>
                <div className="space-y-5 text-sm">
                  <div className="grid gap-3 sm:grid-cols-3">
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-3">
                      <p className="text-xs uppercase tracking-wider text-[var(--app-muted)]">
                        Passing
                      </p>
                      <p className="mt-1 text-base font-semibold text-emerald-500">
                        {cypressParsed?.passing_count ?? 0}
                      </p>
                    </div>
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-3">
                      <p className="text-xs uppercase tracking-wider text-[var(--app-muted)]">
                        Failing
                      </p>
                      <p className="mt-1 text-base font-semibold text-rose-500">
                        {cypressParsed?.failing_count ?? 0}
                      </p>
                    </div>
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-3">
                      <p className="text-xs uppercase tracking-wider text-[var(--app-muted)]">
                        Total
                      </p>
                      <p className="mt-1 text-base font-semibold text-[var(--app-text)]">
                        {cypressParsed?.total_tests ?? 0}
                      </p>
                    </div>
                  </div>
                  <div>
                    <p className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                      Request ids seen in tests
                    </p>
                    <p className="mt-1 break-all font-mono text-xs text-[var(--app-text-secondary)]">
                      {(cypressParsed?.request_ids ?? []).join(', ') || 'None'}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                      Failing tests
                    </p>
                    {(cypressParsed?.failed_test_names ?? []).length ? (
                      <ul className="mt-2 list-disc space-y-1 pl-5 text-[var(--app-text-secondary)]">
                        {(cypressParsed?.failed_test_names ?? []).map((name) => (
                          <li key={name}>{name}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="mt-1 text-xs text-[var(--app-muted)]">
                        No failing test names captured.
                      </p>
                    )}
                  </div>
                </div>
              </CollapsibleSection>
            ) : null}

            {activeTab === 'coverage' ? (
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
            ) : null}

            {activeTab === 'artifacts' ? (
              <>
                <CollapsibleSection title="Run folder file structure" defaultOpen>
                  <ArtifactFilePanel text={run.outputTree?.join('\n')} />
                </CollapsibleSection>
                <CollapsibleSection title="Input (input.json file)" defaultOpen>
                  <ArtifactFilePanel
                    text={
                      run.rawFiles['input.json']
                        ? `${JSON.stringify(parseJsonObject(run.rawFiles['input.json']), null, 2)}`
                        : undefined
                    }
                  />
                </CollapsibleSection>
                <CollapsibleSection title="Cypress parsed report">
                  <ArtifactFilePanel
                    text={
                      run.rawFiles['cypress_parsed.json']
                        ? `${JSON.stringify(cypressParsed ?? parseJsonObject(run.rawFiles['cypress_parsed.json']), null, 2)}`
                        : undefined
                    }
                  />
                </CollapsibleSection>
                <CollapsibleSection title="Quali bot logs" defaultOpen>
                  <ArtifactFilePanel
                    text={
                      run.rawFiles['terminal_output.log'] ?? run.rawFiles['flow_pipeline.log']
                    }
                  />
                </CollapsibleSection>
                <CollapsibleSection title="Router logs">
                  <ArtifactFilePanel text={run.rawFiles['router_run.log']} />
                </CollapsibleSection>
                <CollapsibleSection title="Coverage HTML report">
                  {run.coverageReport?.d?.kind === 'coverage_unavailable' ? (
                    <p className="text-xs text-[var(--app-muted)]">
                      Coverage HTML was not generated for this run because no LLVM profile
                      data was produced.
                    </p>
                  ) : coverageHtmlUrl ? (
                    <iframe
                      title="Coverage HTML report"
                      src={coverageHtmlUrl}
                      className="h-[75vh] w-full rounded-xl border border-[var(--app-border)] bg-white"
                    />
                  ) : (
                    <p className="text-xs text-[var(--app-muted)]">
                      Coverage HTML is not available for this run.
                    </p>
                  )}
                </CollapsibleSection>
                <CollapsibleSection title="Coverage run report">
                  <ArtifactFilePanel
                    text={
                      run.rawFiles['coverage_run_report.json']
                        ? `${JSON.stringify(run.coverageReport ?? parseJsonObject(run.rawFiles['coverage_run_report.json']), null, 2)}`
                        : undefined
                    }
                  />
                </CollapsibleSection>
                <CollapsibleSection title="Final report">
                  <ArtifactFilePanel
                    text={
                      run.rawFiles['final_report.json']
                        ? `${JSON.stringify(run.finalReport ?? parseJsonObject(run.rawFiles['final_report.json']), null, 2)}`
                        : undefined
                    }
                  />
                </CollapsibleSection>
              </>
            ) : null}
          </div>
        ) : null}
      </main>
    </div>
  )
}
