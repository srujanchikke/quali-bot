import { useMemo } from 'react'
import { buildPathFlowCoverageBlocks } from '@/lib/pathFlowCoverage'
import type { LoadedRun } from '@/types/reports'

type Props = {
  run: LoadedRun
}

export function PathFlowCodeCoverage({ run }: Props) {
  const blocks = useMemo(
    () =>
      buildPathFlowCoverageBlocks(
        run.pathFlow,
        run.coverageReport,
        run.finalReport,
        run.rawFiles['line_hits.txt'],
        run.rawFiles['lcov.info'],
      ),
    [run],
  )

  if (blocks.length === 0) {
    return (
      <div className="space-y-2 text-sm leading-relaxed text-[var(--app-muted)]">
        <p>
          No source could be prepared for the current path chain. The UI now tries to
          build this from{' '}
          <code className="rounded-md bg-[var(--app-code-bg)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--app-text-secondary)]">
            input.json
          </code>{' '}
          plus the local source files in your `hyperswitch` checkout, but this run still
          did not produce a usable code snippet.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-8">
      {blocks.map((block) => (
        <div key={`${block.file}-${block.functionName}`}>
          <div className="mb-1 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <h3 className="text-sm font-semibold text-[var(--app-text)]">
              {block.functionName}
            </h3>
            <span className="rounded-md bg-[var(--app-accent-muted)] px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--app-accent)]">
              {block.roleLabel}
            </span>
          </div>
          <p className="mb-2 break-all font-mono text-[11px] text-[var(--app-subtle)]">
            {block.file}
          </p>
          <div className="overflow-x-auto rounded-xl border border-[var(--app-border)] bg-[var(--app-code-bg)]">
            <table className="w-full min-w-[min(100%,720px)] border-collapse text-left text-[11px] leading-snug">
              <thead>
                <tr className="border-b border-[var(--app-border)] bg-[var(--app-elevated)] text-[var(--app-muted)]">
                  <th className="w-12 shrink-0 px-2 py-1.5 font-medium">Line</th>
                  <th className="w-14 shrink-0 px-2 py-1.5 font-medium">Hits</th>
                  <th className="px-2 py-1.5 font-medium">Code</th>
                </tr>
              </thead>
              <tbody className="text-[var(--app-text)]">
                {block.lines.map((row) => (
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
        </div>
      ))}
    </div>
  )
}
