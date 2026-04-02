import { buildRunFromRawFiles } from '@/lib/loadRun'
import type { LoadedRun } from '@/types/reports'

export type RunsListResponse = { runs: string[] }
export type RunTreeResponse = { files: string[] }

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function inferSymbolName(functionName: string): string {
  if (functionName.includes('#')) {
    const parts = functionName.split('#')
    return parts[parts.length - 1] || functionName
  }
  return functionName
}

function inferFunctionHints(functionName: string): string[] {
  const hints = new Set<string>()
  const genericMatches = functionName.match(/<([^>]+)>/g) ?? []
  for (const match of genericMatches) {
    for (const part of match.slice(1, -1).split(',')) {
      const trimmed = part.trim()
      if (trimmed) hints.add(trimmed)
      if (trimmed.endsWith('Data')) hints.add(trimmed.replace(/Data$/, ''))
    }
  }
  if (functionName.includes('#')) {
    for (const part of functionName.split('#')) {
      const trimmed = part.trim()
      if (trimmed) hints.add(trimmed)
    }
  }
  return Array.from(hints)
}

function locateDefinitionLine(
  sourceText: string,
  functionName: string,
  fallbackDefLine: number,
): number {
  const lines = sourceText.replace(/\r\n/g, '\n').split('\n')
  const symbol = inferSymbolName(functionName)
  const fnPattern = new RegExp(`\\bfn\\s+${escapeRegex(symbol)}\\b`)
  const hints = inferFunctionHints(functionName)
  const candidates: Array<{ line: number; score: number }> = []

  for (let i = 0; i < lines.length; i += 1) {
    if (!fnPattern.test(lines[i] ?? '')) continue
    const headerWindow = lines.slice(Math.max(0, i - 12), i + 1).join('\n')
    let score = Math.max(0, 200 - Math.abs(i + 1 - fallbackDefLine))
    for (const hint of hints) {
      if (hint && headerWindow.includes(hint)) score += 100
    }
    candidates.push({ line: i + 1, score })
  }

  if (candidates.length === 0) return fallbackDefLine
  candidates.sort((a, b) => b.score - a.score)
  return candidates[0]?.line ?? fallbackDefLine
}

const OPTIONAL_FILES = [
  'input.json',
  'create_organization.json',
  'path_flow.json',
  'run_summary.txt',
  'terminal_output.log',
  'flow_pipeline.log',
  'router_run.log',
  'cypress_parsed.json',
  'lcov.info',
] as const

/**
 * Lists run folder names from `report-generater/output` (dev / vite preview only).
 */
export async function fetchOutputRunIds(): Promise<string[]> {
  const res = await fetch('/api/runs')
  if (!res.ok) {
    throw new Error(`Runs list failed (${res.status})`)
  }
  const data = (await res.json()) as RunsListResponse
  return data.runs ?? []
}

/**
 * Loads artifacts for one run via `/api/run/:id/...`.
 */
export async function loadRunFromOutputFolder(runId: string): Promise<LoadedRun> {
  const enc = encodeURIComponent(runId)
  const base = `/api/run/${enc}`
  const raw: Record<string, string> = {}
  let outputTree: string[] | undefined

  const getText = async (name: string) => {
    const r = await fetch(`${base}/${encodeURIComponent(name)}`)
    if (r.ok) raw[name] = await r.text()
  }

  await getText('final_report.json')
  await getText('coverage_run_report.json')
  await getText('line_hits.txt')
  for (const name of OPTIONAL_FILES) {
    await getText(name)
  }

  const treeRes = await fetch(`/api/run-tree/${enc}`)
  if (treeRes.ok) {
    const treeData = (await treeRes.json()) as RunTreeResponse
    outputTree = treeData.files ?? []
  }

  if (!raw['final_report.json'] && !raw['coverage_run_report.json']) {
    throw new Error(
      `No final_report.json or coverage_run_report.json for run "${runId}"`,
    )
  }

  const run = buildRunFromRawFiles(runId, raw, outputTree)

  if (run.pathFlow?.flows?.length) {
    const uniqueFiles = Array.from(
      new Set(
        run.pathFlow.flows
          .flatMap((flow) => flow.chain)
          .map((step) => step.file)
          .filter(Boolean),
      ),
    )

    const sourceTexts = new Map<string, string>()
    await Promise.all(
      uniqueFiles.map(async (file) => {
        const res = await fetch(`/api/source-file/${encodeURIComponent(file)}`)
        if (res.ok) sourceTexts.set(file, await res.text())
      }),
    )

    for (const flow of run.pathFlow.flows) {
      for (const step of flow.chain) {
        const sourceText = sourceTexts.get(step.file)
        if (sourceText) {
          step.def_line = locateDefinitionLine(sourceText, step.function, step.def_line)
          step.source = sourceText
        }
      }
    }
  }

  if (run.coverageReport?.context?.reachability?.endpoints?.length && run.pathFlow?.endpoints?.length) {
    run.coverageReport.context.reachability.endpoints =
      run.coverageReport.context.reachability.endpoints.map((endpoint, index) => ({
        ...endpoint,
        chain: endpoint.chain ?? run.pathFlow?.endpoints?.[index]?.chain ?? [],
      }))
  }

  return run
}

export async function outputRunsApiAvailable(): Promise<boolean> {
  try {
    const res = await fetch('/api/runs')
    return res.ok
  } catch {
    return false
  }
}
