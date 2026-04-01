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

function parseLcovFileLineHits(lcovRaw: string | undefined, file: string): Map<number, number> {
  const hits = new Map<number, number>()
  if (!lcovRaw || !file) return hits

  const normalizedFile = norm(file)
  let inTargetFile = false

  for (const rawLine of lcovRaw.replace(/\r\n/g, '\n').split('\n')) {
    if (rawLine.startsWith('SF:')) {
      const sf = norm(rawLine.slice(3))
      inTargetFile = sf === normalizedFile || sf.endsWith(normalizedFile)
      continue
    }
    if (!inTargetFile) continue
    if (rawLine.startsWith('DA:')) {
      const [, payload] = rawLine.split(':', 2)
      const [lineStr, hitStr] = payload.split(',', 2)
      const lineNumber = Number.parseInt(lineStr, 10)
      const hitCount = Number.parseInt(hitStr, 10)
      if (!Number.isNaN(lineNumber) && !Number.isNaN(hitCount)) {
        hits.set(lineNumber, hitCount)
      }
      continue
    }
    if (rawLine === 'end_of_record') {
      inTargetFile = false
    }
  }

  return hits
}

function extractFunctionSnippet(
  sourceText: string,
  defLine: number,
  bodySpan?: { start: number; end: number },
): Array<{ lineNumber: number; text: string }> {
  const allLines = sourceText.replace(/\r\n/g, '\n').split('\n')
  if (allLines.length === 0) return []

  const firstMeaningfulLine =
    allLines.find((line) => line.trim().length > 0)?.trim() ?? ''
  const looksLikeSnippet =
    defLine > allLines.length ||
    /^(pub\s+)?(async\s+)?fn\b/.test(firstMeaningfulLine) ||
    /^fn\b/.test(firstMeaningfulLine)

  if (looksLikeSnippet) {
    return allLines.map((text, idx) => ({
      lineNumber: defLine + idx,
      text,
    }))
  }

  if (bodySpan) {
    const start = Math.max(1, defLine)
    const end = Math.min(allLines.length, bodySpan.end)
    const rows: Array<{ lineNumber: number; text: string }> = []
    for (let lineNumber = start; lineNumber <= end; lineNumber += 1) {
      rows.push({ lineNumber, text: allLines[lineNumber - 1] ?? '' })
    }
    return rows
  }

  const startIndex = Math.max(0, defLine - 1)
  let started = false
  let braceDepth = 0
  const rows: Array<{ lineNumber: number; text: string }> = []

  for (let index = startIndex; index < allLines.length && rows.length < 160; index += 1) {
    const text = allLines[index] ?? ''
    rows.push({ lineNumber: index + 1, text })

    for (const char of text) {
      if (char === '{') {
        braceDepth += 1
        started = true
      } else if (char === '}') {
        braceDepth = Math.max(0, braceDepth - 1)
      }
    }

    if (started && braceDepth === 0 && index > startIndex) {
      break
    }
  }

  return rows
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
  lcovRaw?: string | undefined,
): PathFlowFunctionBlock[] {
  const flow = pathFlow?.flows?.[0]
  const chain: PathFlowChainStep[] = flow?.chain ?? []
  if (chain.length === 0) return []

  const gaps = coverageReport?.d?.gaps ?? []
  const lineHitMap = lineHitsRaw ? parseLineHitsText(lineHitsRaw) : new Map()

  const blocks: PathFlowFunctionBlock[] = []

  for (const step of chain) {
    if (!step.source?.trim()) continue

    const gap = gapForStep(step, gaps, finalReport)
    const snippetRows = extractFunctionSnippet(step.source, step.def_line, gap?.body_span)
    const lcovHitMap = parseLcovFileLineHits(lcovRaw, step.file)

    const lines: CoverageLineView[] = snippetRows.map(({ lineNumber, text }) => {
      let hits: number | null = null

      if (lineHitMap.has(lineNumber)) {
        hits = lineHitMap.get(lineNumber)!
      } else if (lcovHitMap.has(lineNumber)) {
        hits = lcovHitMap.get(lineNumber)!
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
