export type ApiError = {
  type?: string
  code?: string
  message?: string
}

export type FinalReport = {
  request_id?: string
  api_call?: {
    method?: string
    endpoint?: string
    api_flow?: string
    http_status_code?: number
    error?: ApiError
  }
  router_log_correlation?: {
    router_log_path?: string
    matches_in_log?: number
    sample_lines?: { line_no: number; line: string }[]
  }
  coverage_diff?: {
    leaf?: { name?: string; file?: string; def_line?: number }
    gap_status?: string
    line_coverage_ratio?: number
    zero_hit_lines?: number[]
    body_span?: { start: number; end: number }
    lines_in_span?: number
    lcov_hit_lines?: number
  }
  root_cause_analysis?: {
    status?: string
    reason?: string
    why_coverage_is_zero?: string
  }
}

export type PathFlowChainStep = {
  function: string
  file: string
  def_line: number
  role: string
  source: string
}

export type PathFlowArtifact = {
  function?: string
  file?: string
  def_line?: number
  endpoints?: {
    method: string
    path: string
    handler: string
    chain: string[]
  }[]
  flows?: {
    flow_id: number
    description?: string
    endpoints?: { method: string; path: string; handler: string }[]
    chain: PathFlowChainStep[]
  }[]
}

export type CoverageGap = {
  function?: string
  file?: string
  role?: string
  def_line?: number
  body_span?: { start: number; end: number }
  lines_in_span?: number
  zero_hit_lines?: number[]
  lcov_hit_lines?: number
  line_coverage_ratio?: number
  status?: string
  lines_without_lcov_da?: number
  lcov_probed_lines?: number
  note?: string | null
}

/** Explains how path-flow relates to coverage scoring (from tooling JSON). */
export type PathFlowModelNotes = {
  scored_for_coverage?: string
  leaf?: string
  chain?: string
  objective?: string
}

export type CoverageRunReport = {
  pipeline?: string
  lcov_path?: string
  path_flow_model?: PathFlowModelNotes
  pl?: unknown[]
  run_records?: unknown[]
  d?: {
    kind?: string
    error?: unknown
    leaf?: { name?: string; file?: string; def_line?: number }
    gaps?: CoverageGap[]
  }
  context?: {
    path_flow_model?: PathFlowModelNotes
    leaf?: { name?: string; file?: string; def_line?: number }
    reachability?: {
      endpoint_count?: number
      endpoints?: {
        method: string
        path: string
        handler: string
        chain: string[]
      }[]
    }
    chain_artifact_path?: string
  }
  LEAF_public?: { name?: string; file?: string; def_line?: number }
  CHAIN_ARTIFACT?: string
  audit_trail?: unknown
}

export type LoadedRun = {
  id: string
  finalReport?: FinalReport
  pathFlow?: PathFlowArtifact
  coverageReport?: CoverageRunReport
  /** Original filenames → contents */
  rawFiles: Record<string, string>
}
