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
  if (
    lower.includes('create_organization') ||
    lower.includes('path_flow') ||
    lower === 'input.json'
  )
    return 'path_flow.json'
  return null
}

export function buildRunFromRawFiles(
  id: string,
  rawFiles: Record<string, string>,
  outputTree?: string[],
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

  if (!pathFlow && rawFiles['input.json']) {
    try {
      const input = parseJson<{
        endpoints?: Array<{
          method?: string
          path?: string
          handler?: string
          call_chain?: string[]
        }>
        flows?: Array<{
          flow_id?: number
          description?: string
          endpoints?: Array<{ method?: string; path?: string; handler?: string }>
          chain?: Array<{
            function?: string
            file?: string
            def_line?: number
            role?: string
            source?: string
          }>
        }>
      }>(rawFiles['input.json'])

      pathFlow = {
        endpoints: (input.endpoints ?? []).map((ep) => ({
          method: ep.method ?? 'UNKNOWN',
          path: ep.path ?? 'UNKNOWN',
          handler: ep.handler ?? '',
          chain: ep.call_chain ?? [],
        })),
        flows: (input.flows ?? []).map((flow, flowIndex) => ({
          flow_id: flow.flow_id ?? flowIndex + 1,
          description: flow.description,
          endpoints: (flow.endpoints ?? []).map((ep) => ({
            method: ep.method ?? 'UNKNOWN',
            path: ep.path ?? 'UNKNOWN',
            handler: ep.handler ?? '',
          })),
          chain: (flow.chain ?? []).map((step, stepIndex, allSteps) => ({
            function: step.function ?? `step_${stepIndex + 1}`,
            file: step.file ?? '',
            def_line: step.def_line ?? 0,
            role:
              step.role ??
              (stepIndex === allSteps.length - 1 ? 'target' : 'context'),
            source: step.source ?? '',
          })),
        })),
      }
    } catch {
      /* skip invalid input.json */
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

  return { id, finalReport, pathFlow, coverageReport, outputTree, rawFiles }
}
