import type {
  CoverageRunReport,
  FinalReport,
  LoadedRun,
  PathFlowArtifact,
} from '@/types/reports'

export function parseJson<T>(text: string): T {
  return JSON.parse(text) as T
}

export function inferArtifactName(filename: string): keyof LoadedRun['rawFiles'] | null {
  const lower = filename.toLowerCase()
  if (lower.includes('final_report')) return 'final_report.json'
  if (lower.includes('coverage_run_report')) return 'coverage_run_report.json'
  if (lower.includes('create_organization') || lower.includes('path_flow'))
    return 'path_flow.json'
  return null
}

export function buildRunFromRawFiles(
  id: string,
  rawFiles: Record<string, string>,
): LoadedRun {
  let finalReport: FinalReport | undefined
  let pathFlow: PathFlowArtifact | undefined
  let coverageReport: CoverageRunReport | undefined

  for (const [name, text] of Object.entries(rawFiles)) {
    const key = inferArtifactName(name)
    if (!key) continue
    try {
      if (key === 'final_report.json') finalReport = parseJson<FinalReport>(text)
      if (key === 'path_flow.json') pathFlow = parseJson<PathFlowArtifact>(text)
      if (key === 'coverage_run_report.json')
        coverageReport = parseJson<CoverageRunReport>(text)
    } catch {
      /* skip invalid */
    }
  }

  if (!pathFlow && coverageReport?.context?.reachability?.endpoints?.[0]) {
    const ep = coverageReport.context.reachability.endpoints[0]
    const names = ep.chain ?? []
    pathFlow = {
      endpoints: [
        {
          method: ep.method,
          path: ep.path,
          handler: ep.handler,
          chain: names,
        },
      ],
      flows: [
        {
          flow_id: 1,
          chain: names.map((fn, i) => ({
            function: fn,
            file: '',
            def_line: 0,
            role: i === names.length - 1 ? 'target' : 'context',
            source: '',
          })),
        },
      ],
    }
  }

  return { id, finalReport, pathFlow, coverageReport, rawFiles }
}
