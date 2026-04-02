import type { ReactNode } from 'react'
import { buildPathFlowCoverageBlocks } from '@/lib/pathFlowCoverage'
import type { LoadedRun } from '@/types/reports'

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <h3 className="text-xs font-semibold uppercase tracking-wider text-[var(--app-muted)]">
      {children}
    </h3>
  )
}

function Field({
  label,
  children,
}: {
  label: string
  children: ReactNode
}) {
  return (
    <div>
      <p className="text-[11px] font-medium text-[var(--app-subtle)]">{label}</p>
      <div className="mt-1 text-sm leading-relaxed text-[var(--app-text)]">
        {children}
      </div>
    </div>
  )
}

function humanizeGapStatus(status: string | undefined): string {
  if (!status) return '—'
  const map: Record<string, string> = {
    all_probed_lines_zero:
      'Every line that was checked recorded zero executions (none of those lines were hit).',
    all_probed_lines_hit:
      'Every line that was checked was executed at least once.',
  }
  return map[status] ?? status.replace(/_/g, ' ')
}

function coverageTone(percentage: number | null) {
  if (percentage == null) return 'border-slate-500/20 bg-slate-500/8 text-[var(--app-text)]'
  if (percentage === 100) return 'border-emerald-500/20 bg-emerald-500/10 text-emerald-400'
  if (percentage > 0) return 'border-amber-500/20 bg-amber-500/10 text-amber-400'
  return 'border-rose-500/20 bg-rose-500/10 text-rose-400'
}

function formatPercentage(value: number | null) {
  if (value == null || !Number.isFinite(value)) return 'No measurable lines'
  return `${value.toFixed(1)}%`
}

function MetricPill({
  value,
  label,
}: {
  value: string | number
  label: string
}) {
  return (
    <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] px-3 py-2">
      <p className="text-sm font-semibold text-[var(--app-text)]">{value}</p>
      <p className="mt-0.5 text-[11px] uppercase tracking-wide text-[var(--app-muted)]">
        {label}
      </p>
    </div>
  )
}

type Props = {
  run: LoadedRun
}

export function CoverageReportHumanView({ run }: Props) {
  const report = run.coverageReport
  if (!report) {
    return (
      <p className="text-sm text-[var(--app-muted)]">
        No coverage report is loaded for this run.
      </p>
    )
  }

  const d = report.d
  const reach = report.context?.reachability
  const fallbackChain = run.pathFlow?.flows?.[0]?.chain?.map((step) => step.function) ?? []
  const endpoints =
    reach?.endpoints?.map((endpoint, index) => ({
      ...endpoint,
      chain:
        endpoint.chain?.length
          ? endpoint.chain
          : run.pathFlow?.endpoints?.[index]?.chain?.length
            ? run.pathFlow.endpoints[index]!.chain
            : fallbackChain,
    })) ?? []
  const blocks = buildPathFlowCoverageBlocks(
    run.pathFlow,
    run.coverageReport,
    run.finalReport,
    run.rawFiles['line_hits.txt'],
    run.rawFiles['lcov.info'],
  )

  const functionSummaries = blocks.map((block) => {
    const measurableLines = block.lines.filter((line) => line.hits !== null)
    const hitLines = measurableLines.filter((line) => (line.hits ?? 0) > 0)
    const zeroHitLines = measurableLines.filter((line) => line.hits === 0)
    const percentage =
      measurableLines.length > 0 ? (hitLines.length / measurableLines.length) * 100 : null

    return {
      ...block,
      measurableCount: measurableLines.length,
      hitCount: hitLines.length,
      zeroHitCount: zeroHitLines.length,
      percentage,
    }
  })

  const measuredFunctions = functionSummaries.filter((item) => item.measurableCount > 0)
  const overallMeasuredLines = measuredFunctions.reduce(
    (sum, item) => sum + item.measurableCount,
    0,
  )
  const overallHitLines = measuredFunctions.reduce((sum, item) => sum + item.hitCount, 0)
  const overallPercentage =
    overallMeasuredLines > 0 ? (overallHitLines / overallMeasuredLines) * 100 : null
  const zeroHitLinesTotal = measuredFunctions.reduce((sum, item) => sum + item.zeroHitCount, 0)
  const chainFunctionCount = functionSummaries.length
  const fullyCoveredFunctions = measuredFunctions.filter((item) => item.percentage === 100).length
  const uncoveredFunctions = measuredFunctions.filter((item) => item.hitCount === 0).length
  const functionsWithoutMeasuredLines = functionSummaries.filter(
    (item) => item.measurableCount === 0,
  ).length

  return (
    <div className="space-y-6">
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
          <SectionTitle>Overall flow coverage</SectionTitle>
          <p className="mt-3 text-2xl font-semibold text-[var(--app-text)]">
            {formatPercentage(overallPercentage)}
          </p>
          <p className="mt-2 text-sm text-[var(--app-text-secondary)]">
            {overallMeasuredLines
              ? `${overallHitLines} of ${overallMeasuredLines} measurable lines on the shown chain were hit.`
              : 'No measurable lines were found for the shown chain in this run.'}
          </p>
        </div>
        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
          <SectionTitle>Functions measured on this flow</SectionTitle>
          <p className="mt-3 text-2xl font-semibold text-[var(--app-text)]">
            {chainFunctionCount}
          </p>
          <p className="mt-2 text-sm text-[var(--app-text-secondary)]">
            {measuredFunctions.length
              ? `${fullyCoveredFunctions} fully covered, ${uncoveredFunctions} with no hit lines, ${Math.max(measuredFunctions.length - fullyCoveredFunctions - uncoveredFunctions, 0)} partially covered.`
              : 'The chain was identified, but this run did not measure any function lines yet.'}
          </p>
        </div>
        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
          <SectionTitle>Measured lines summary</SectionTitle>
          <p className="mt-3 text-2xl font-semibold text-[var(--app-text)]">
            {overallHitLines} / {zeroHitLinesTotal}
          </p>
          <p className="mt-2 text-sm text-[var(--app-text-secondary)]">
            Hit lines / zero-hit lines across the measured part of this flow.
            {functionsWithoutMeasuredLines > 0
              ? ` ${functionsWithoutMeasuredLines} function${functionsWithoutMeasuredLines === 1 ? '' : 's'} on the chain still had no measurable lines in this run.`
              : ''}
          </p>
        </div>
      </div>

      <div className="space-y-3">
        <SectionTitle>Function coverage on this flow</SectionTitle>
        <div className="grid gap-4 lg:grid-cols-2">
          {functionSummaries.map((item) => (
            <div
              key={`${item.file}-${item.functionName}`}
              className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-[var(--app-text)]">{item.functionName}</p>
                  <p className="mt-1 break-all font-mono text-xs text-[var(--app-subtle)]">
                    {item.file}
                  </p>
                </div>
                <span
                  className={`rounded-full border px-2.5 py-1 text-xs font-medium ${coverageTone(item.percentage)}`}
                >
                  {formatPercentage(item.percentage)}
                </span>
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-[var(--app-text-secondary)]">
                <span className="rounded-md bg-[var(--app-accent-muted)] px-1.5 py-0.5 font-medium uppercase tracking-wide text-[var(--app-accent)]">
                  {item.roleLabel}
                </span>
              </div>
              <div className="mt-4 grid gap-2 sm:grid-cols-3">
                <MetricPill value={item.hitCount} label="Covered" />
                <MetricPill value={item.zeroHitCount} label="Missed" />
                <MetricPill value={item.measurableCount} label="Total" />
              </div>
            </div>
          ))}
        </div>
      </div>

      {endpoints.length > 0 ? (
        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
          <SectionTitle>Flow entry points</SectionTitle>
          <div className="mt-4 space-y-4">
            {endpoints.map((ep, i) => (
              <div
                key={`${ep.method}-${ep.path}-${i}`}
                className="rounded-lg border border-[var(--app-border)] bg-[var(--app-elevated)] p-4"
              >
                <div className="space-y-3">
                  <div>
                    <p className="text-[11px] font-medium text-[var(--app-subtle)]">API call</p>
                    <p className="mt-1 text-sm leading-relaxed text-[var(--app-text)]">
                      {ep.method} {ep.path}
                    </p>
                  </div>
                  <div>
                    <p className="text-[11px] font-medium text-[var(--app-subtle)]">Handler</p>
                    <p className="mt-1 text-sm leading-relaxed text-[var(--app-text)]">{ep.handler}</p>
                  </div>
                  <div>
                    <p className="text-[11px] font-medium text-[var(--app-subtle)]">Chain</p>
                    <p className="mt-1 break-words text-sm leading-relaxed text-[var(--app-text-secondary)]">
                      {ep.chain?.length ? ep.chain.join(' -> ') : 'No chain details were available.'}
                    </p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

    </div>
  )
}
