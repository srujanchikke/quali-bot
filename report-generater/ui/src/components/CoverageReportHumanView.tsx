import type { ReactNode } from 'react'
import type { CoverageGap, CoverageRunReport } from '@/types/reports'
import { displayChainRoleLabel } from '@/lib/chainRoleLabel'

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

function LeafBlock({
  title,
  leaf,
}: {
  title: string
  leaf: { name?: string; file?: string; def_line?: number } | undefined
}) {
  return (
    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
      <SectionTitle>{title}</SectionTitle>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <Field label="Function name">{leaf?.name ?? '—'}</Field>
        <Field label="Source file path">
          <span className="break-all font-mono text-xs">{leaf?.file ?? '—'}</span>
        </Field>
        <Field label="Line where the function definition starts">
          {leaf?.def_line != null ? String(leaf.def_line) : '—'}
        </Field>
      </div>
    </div>
  )
}

function GapCard({ gap, index }: { gap: CoverageGap; index: number }) {
  const ratio = gap.line_coverage_ratio
  const pct =
    ratio != null && Number.isFinite(ratio)
      ? `${(ratio <= 1 ? ratio * 100 : ratio).toFixed(1)}%`
      : '—'

  return (
    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
      <p className="text-sm font-medium text-[var(--app-text)]">
        Gap {index + 1}
        {gap.function ? `: ${gap.function}` : ''}
      </p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <Field label="Function name">{gap.function ?? '—'}</Field>
        <Field label="Role in the path (how this function is classified)">
          {gap.role ? displayChainRoleLabel(gap.role) : '—'}
        </Field>
        <Field label="Source file path">
          <span className="break-all font-mono text-xs">{gap.file ?? '—'}</span>
        </Field>
        <Field label="Line where the function definition starts">
          {gap.def_line != null ? String(gap.def_line) : '—'}
        </Field>
        <Field label="Function body line range (first line through last line)">
          {gap.body_span
            ? `From line ${gap.body_span.start} through line ${gap.body_span.end}`
            : '—'}
        </Field>
        <Field label="Number of source lines inside that body range">
          {gap.lines_in_span != null ? String(gap.lines_in_span) : '—'}
        </Field>
        {gap.lines_without_lcov_da != null ? (
          <Field label="Lines in the body that have no coverage data entry">
            {String(gap.lines_without_lcov_da)}
          </Field>
        ) : null}
        <Field label="Lines matched in the coverage data file for this body">
          {gap.lcov_probed_lines != null
            ? String(gap.lcov_probed_lines)
            : '—'}
        </Field>
        <Field label="Lines in the body that appear in the coverage data file with a hit count">
          {gap.lcov_hit_lines != null ? String(gap.lcov_hit_lines) : '—'}
        </Field>
        <Field label="Share of probed lines that were executed at least once">
          {pct}
        </Field>
        <Field label="Outcome for this gap">{humanizeGapStatus(gap.status)}</Field>
        {gap.note ? (
          <Field label="Note">
            <span>{gap.note}</span>
          </Field>
        ) : null}
      </div>
    </div>
  )
}

type Props = {
  report: CoverageRunReport | undefined
}

export function CoverageReportHumanView({ report }: Props) {
  if (!report) {
    return (
      <p className="text-sm text-[var(--app-muted)]">
        No coverage report is loaded for this run.
      </p>
    )
  }

  const d = report.d
  const gaps = d?.gaps ?? []
  const reach = report.context?.reachability
  const endpoints = reach?.endpoints ?? []
  const leafPrimary = d?.leaf ?? report.context?.leaf
  const leafPublic = report.LEAF_public
  const leafPublicIsDuplicate =
    leafPublic &&
    leafPrimary &&
    leafPublic.name === leafPrimary.name &&
    leafPublic.file === leafPrimary.file &&
    leafPublic.def_line === leafPrimary.def_line

  return (
    <div className="space-y-6">
      <LeafBlock title="Leaf function (primary target)" leaf={leafPrimary} />

      {gaps.length > 0 ? (
        <div className="space-y-3">
          <SectionTitle>Coverage gaps (detail per measured function)</SectionTitle>
          <div className="space-y-4">
            {gaps.map((gap, i) => (
              <GapCard key={`${gap.file}-${gap.function}-${i}`} gap={gap} index={i} />
            ))}
          </div>
        </div>
      ) : (
        <p className="text-sm text-[var(--app-muted)]">
          No per-function gap list was included in this report.
        </p>
      )}

      {endpoints.length > 0 ? (
        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
          <SectionTitle>Reachable HTTP routes and call chain</SectionTitle>
          <p className="mt-1 text-xs text-[var(--app-muted)]">
            Number of endpoints described: {reach?.endpoint_count ?? endpoints.length}
          </p>
          <ul className="mt-4 space-y-4">
            {endpoints.map((ep, i) => (
              <li
                key={`${ep.method}-${ep.path}-${i}`}
                className="rounded-lg border border-[var(--app-border)] bg-[var(--app-elevated)] p-3"
              >
                <Field label="HTTP method">{ep.method}</Field>
                <Field label="URL path">{ep.path}</Field>
                <Field label="Handler function name">{ep.handler}</Field>
                <Field label="Ordered chain of function names from route to leaf">
                  {ep.chain?.length ? ep.chain.join(' → ') : '—'}
                </Field>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {leafPublic &&
      (leafPublic.name || leafPublic.file) &&
      !leafPublicIsDuplicate ? (
        <LeafBlock
          title="Published leaf summary (second copy in the report file)"
          leaf={leafPublic}
        />
      ) : null}

      {d?.error != null && d.error !== false ? (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4">
          <SectionTitle>Error from the coverage step</SectionTitle>
          <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono text-xs text-[var(--app-text)]">
            {typeof d.error === 'string'
              ? d.error
              : JSON.stringify(d.error, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  )
}
