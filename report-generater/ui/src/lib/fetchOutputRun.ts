import { buildRunFromRawFiles } from '@/lib/loadRun'
import type { LoadedRun } from '@/types/reports'

export type RunsListResponse = { runs: string[] }

const OPTIONAL_FILES = [
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

  if (!raw['final_report.json'] && !raw['coverage_run_report.json']) {
    throw new Error(
      `No final_report.json or coverage_run_report.json for run "${runId}"`,
    )
  }

  return buildRunFromRawFiles(runId, raw)
}

export async function outputRunsApiAvailable(): Promise<boolean> {
  try {
    const res = await fetch('/api/runs')
    return res.ok
  } catch {
    return false
  }
}
