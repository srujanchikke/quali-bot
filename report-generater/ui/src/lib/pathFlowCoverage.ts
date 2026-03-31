import { displayChainRoleLabel } from '@/lib/chainRoleLabel'
import { parseLineHitsText } from '@/lib/parseLineHits'
import type {
  CoverageGap,
  CoverageRunReport,
  FinalReport,
  PathFlowArtifact,
  PathFlowChainStep,
} from '@/types/reports'

export type CoverageLineView = {
  lineNumber: number
  text: string
  hits: number | null
}

export type PathFlowFunctionBlock = {
  functionName: string
  file: string
  /** Display label (Chain / Leaf / …), not raw artifact role. */
  roleLabel: string
  lines: CoverageLineView[]
}

function norm(p: string): string {
  return p.replace(/\\/g, '/').trim()
}

function findGap(
  gaps: CoverageGap[] | undefined,
  file: string,
): CoverageGap | undefined {
  if (!gaps?.length || !file) return undefined
  const n = norm(file)
  return (
    gaps.find((g) => g.file && norm(g.file) === n) ??
    gaps.find((g) => g.file && n.endsWith(norm(g.file)))
  )
}

function gapForStep(
  step: PathFlowChainStep,
  gaps: CoverageGap[],
  finalReport: FinalReport | undefined,
): CoverageGap | undefined {
  const g = findGap(gaps, step.file)
  if (g) return g

  const leaf = finalReport?.coverage_diff?.leaf
  const span = finalReport?.coverage_diff?.body_span
  const zeros = finalReport?.coverage_diff?.zero_hit_lines
  if (leaf?.file && span && norm(step.file) === norm(leaf.file)) {
    return {
      file: leaf.file,
      body_span: span,
      zero_hit_lines: zeros ?? [],
    } as CoverageGap
  }
  return undefined
}

/**
 * LLVM-style rows for each chain step that includes embedded `source`,
 * scoped to files in the path-flow artifact. Hits merge `line_hits.txt`,
 * `coverage_run_report.d.gaps`, and `final_report.coverage_diff` (leaf).
 */
export function buildPathFlowCoverageBlocks(
  pathFlow: PathFlowArtifact | undefined,
  coverageReport: CoverageRunReport | undefined,
  finalReport: FinalReport | undefined,
  lineHitsRaw: string | undefined,
): PathFlowFunctionBlock[] {
  const flow = pathFlow?.flows?.[0]
  const chain: PathFlowChainStep[] = flow?.chain ?? []
  if (chain.length === 0) return []

  const gaps = coverageReport?.d?.gaps ?? []
  const lineHitMap = lineHitsRaw ? parseLineHitsText(lineHitsRaw) : new Map()

  const blocks: PathFlowFunctionBlock[] = []

  for (const step of chain) {
    if (!step.source?.trim()) continue

    const rows = step.source.replace(/\r\n/g, '\n').split('\n')
    const gap = gapForStep(step, gaps, finalReport)

    const lines: CoverageLineView[] = rows.map((text, idx) => {
      const lineNumber = step.def_line + idx
      let hits: number | null = null

      if (lineHitMap.has(lineNumber)) {
        hits = lineHitMap.get(lineNumber)!
      } else if (
        gap?.body_span &&
        lineNumber >= gap.body_span.start &&
        lineNumber <= gap.body_span.end
      ) {
        const z = gap.zero_hit_lines ?? []
        hits = z.includes(lineNumber) ? 0 : 1
      }

      return { lineNumber, text, hits }
    })

    blocks.push({
      functionName: step.function,
      file: step.file,
      roleLabel: displayChainRoleLabel(step.role),
      lines,
    })
  }

  return blocks
}
