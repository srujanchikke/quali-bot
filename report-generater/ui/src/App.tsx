import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import { CollapsibleSection } from '@/components/CollapsibleSection'
import { FlowBreadcrumb } from '@/components/FlowBreadcrumb'
import { FlowCanvas } from '@/components/FlowCanvas'
import { CoverageReportHumanView } from '@/components/CoverageReportHumanView'
import { PathFlowCodeCoverage } from '@/components/PathFlowCodeCoverage'
import { RunPickerPanel } from '@/components/RunPickerPanel'
import {
  fetchOutputRunIds,
  loadRunFromOutputFolder,
} from '@/lib/fetchOutputRun'
import { buildPathFlowCoverageBlocks } from '@/lib/pathFlowCoverage'
import type { LoadedRun } from '@/types/reports'
import { useTheme } from '@/theme/useTheme'
import { ChevronDown, Moon, Sun } from 'lucide-react'

type RunTab = 'overview' | 'testing' | 'coverage' | 'artifacts'

type AnsiStyleState = {
  fg?: string
  bold?: boolean
  dim?: boolean
  italic?: boolean
}

const ANSI_SGR_PATTERN = /\u001b\[([0-9;]*)m/g

const ANSI_COLORS: Record<number, string> = {
  30: '#94a3b8',
  31: '#f87171',
  32: '#4ade80',
  33: '#fbbf24',
  34: '#60a5fa',
  35: '#c084fc',
  36: '#22d3ee',
  37: '#e5e7eb',
  90: '#64748b',
  91: '#fca5a5',
  92: '#86efac',
  93: '#fcd34d',
  94: '#93c5fd',
  95: '#d8b4fe',
  96: '#67e8f9',
  97: '#f8fafc',
}

function parseJsonObject(text: string | undefined): unknown | undefined {
  if (!text) return undefined
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

function applyAnsiCode(state: AnsiStyleState, code: number): AnsiStyleState {
  if (code === 0) return {}
  if (code === 1) return { ...state, bold: true }
  if (code === 2) return { ...state, dim: true }
  if (code === 3) return { ...state, italic: true }
  if (code === 22) return { ...state, bold: false, dim: false }
  if (code === 23) return { ...state, italic: false }
  if (code === 39) return { ...state, fg: undefined }
  if (ANSI_COLORS[code]) return { ...state, fg: ANSI_COLORS[code] }
  return state
}

function ansiStyleToCss(style: AnsiStyleState) {
  return {
    color: style.fg,
    fontWeight: style.bold ? 700 : undefined,
    opacity: style.dim ? 0.72 : undefined,
    fontStyle: style.italic ? 'italic' : undefined,
  }
}

function renderAnsiText(text: string) {
  const lines = text.split('\n')

  return lines.map((line, lineIndex) => {
    const segments: Array<{ text: string; style: AnsiStyleState }> = []
    let style: AnsiStyleState = {}
    let cursor = 0

    for (const match of line.matchAll(ANSI_SGR_PATTERN)) {
      const matchIndex = match.index ?? 0
      if (matchIndex > cursor) {
        segments.push({
          text: line.slice(cursor, matchIndex),
          style,
        })
      }

      const codes = (match[1] || '0')
        .split(';')
        .map((part) => Number.parseInt(part || '0', 10))

      for (const code of codes) {
        style = applyAnsiCode(style, Number.isNaN(code) ? 0 : code)
      }

      cursor = matchIndex + match[0].length
    }

    if (cursor < line.length) {
      segments.push({
        text: line.slice(cursor),
        style,
      })
    }

    if (segments.length === 0) {
      segments.push({ text: '', style: {} })
    }

    return (
      <Fragment key={lineIndex}>
        {segments.map((segment, segmentIndex) => (
          <span
            key={`${lineIndex}-${segmentIndex}`}
            style={ansiStyleToCss(segment.style)}
          >
            {segment.text || (segmentIndex === 0 ? ' ' : '')}
          </span>
        ))}
        {lineIndex < lines.length - 1 ? '\n' : null}
      </Fragment>
    )
  })
}

function JsonToken({ children, className }: { children: string; className: string }) {
  return <span className={className}>{children}</span>
}

function renderMultilineJsonString(
  source: string,
  indentLevel: number,
  nodePath: string,
): React.ReactNode[] {
  const indent = '  '.repeat(indentLevel)
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const nodes: React.ReactNode[] = ['"\n']

  lines.forEach((line, index) => {
    nodes.push(
      <Fragment key={`source-line-${nodePath}-${index}`}>
        {indent}  <span className="font-mono text-[12px] text-[var(--app-json-source)]">{line || ' '}</span>
      </Fragment>,
    )
    nodes.push(index < lines.length - 1 ? '\n' : '')
  })

  nodes.push(`\n${indent}"`)
  return nodes
}

function renderJsonValue(
  value: unknown,
  indentLevel = 0,
  parentKey?: string,
  nodePath = 'root',
): React.ReactNode[] {
  const indent = '  '.repeat(indentLevel)
  const nextIndent = '  '.repeat(indentLevel + 1)

  if (value === null) {
    return [
      <JsonToken
        key={`${nodePath}-null`}
        className="text-[var(--app-json-null)]"
        children="null"
      />,
    ]
  }

  if (typeof value === 'string') {
    if (parentKey === 'source' && value.includes('\n')) {
      return renderMultilineJsonString(value, indentLevel, nodePath)
    }
    return [
      <JsonToken
        key={`${nodePath}-string`}
        className="text-[var(--app-json-string)]"
        children={JSON.stringify(value)}
      />,
    ]
  }

  if (typeof value === 'number') {
    return [
      <JsonToken
        key={`${nodePath}-number`}
        className="font-medium text-[var(--app-json-number)]"
        children={String(value)}
      />,
    ]
  }

  if (typeof value === 'boolean') {
    return [
      <JsonToken
        key={`${nodePath}-boolean`}
        className="font-medium text-[var(--app-json-boolean)]"
        children={String(value)}
      />,
    ]
  }

  if (Array.isArray(value)) {
    if (value.length === 0) return ['[]']
    const nodes: React.ReactNode[] = ['[\n']
    value.forEach((item, index) => {
      const childPath = `${nodePath}[${index}]`
      nodes.push(<Fragment key={`arr-indent-${childPath}`}>{nextIndent}</Fragment>)
      nodes.push(...renderJsonValue(item, indentLevel + 1, undefined, childPath))
      nodes.push(index < value.length - 1 ? ',\n' : '\n')
    })
    nodes.push(`${indent}]`)
    return nodes
  }

  if (typeof value === 'object') {
    const entries = Object.entries(value)
    if (entries.length === 0) return ['{}']
    const nodes: React.ReactNode[] = ['{\n']
    entries.forEach(([key, entryValue], index) => {
      const childPath = `${nodePath}.${key}`
      nodes.push(<Fragment key={`obj-indent-${childPath}`}>{nextIndent}</Fragment>)
      nodes.push(
        <JsonToken
          key={`obj-key-${childPath}`}
          className="text-[var(--app-json-key)]"
          children={JSON.stringify(key)}
        />,
      )
      nodes.push(': ')
      nodes.push(...renderJsonValue(entryValue, indentLevel + 1, key, childPath))
      nodes.push(index < entries.length - 1 ? ',\n' : '\n')
    })
    nodes.push(`${indent}}`)
    return nodes
  }

  return [String(value)]
}

function renderStructuredText(text: string) {
  const parsed = parseJsonObject(text)
  if (parsed === undefined) {
    return renderAnsiText(text)
  }
  return renderJsonValue(parsed)
}

function ArtifactFilePanel({
  text,
}: {
  text: string | undefined
}) {
  return (
    <section>
      {!text ? (
        <p className="text-xs text-[var(--app-muted)]">Not generated for this run.</p>
      ) : (
        <pre className="max-h-[min(72vh,640px)] overflow-auto whitespace-pre-wrap break-words rounded-xl border border-[var(--app-border)] bg-[var(--app-code-bg)] p-4 text-[12px] leading-relaxed text-[var(--app-text)] shadow-inner">
          {renderStructuredText(text)}
        </pre>
      )}
    </section>
  )
}

function statusTone(status: 'ok' | 'partial' | 'failed') {
  if (status === 'ok') {
    return 'border-emerald-500/20 bg-emerald-500/10 text-emerald-400'
  }
  if (status === 'failed') {
    return 'border-rose-500/20 bg-rose-500/10 text-rose-400'
  }
  return 'border-amber-500/20 bg-amber-500/10 text-amber-400'
}

function shortFileName(path: string | undefined) {
  if (!path) return undefined
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

function includesAny(text: string, needles: string[]) {
  const lower = text.toLowerCase()
  return needles.some((needle) => lower.includes(needle.toLowerCase()))
}

type ParsedTestingSpec = {
  spec: string
  tests: number
  passing: number
  failing: number
  pending: number
  skipped: number
  screenshots: number
  videoEnabled: boolean | null
  videoOutput: string | null
  duration: string
  suiteTitles: string[]
  passingNotes: string[]
  failedTests: string[]
  requestIds: string[]
  stepNames: string[]
  methods: string[]
  urls: string[]
  responseStatuses: string[]
  responseMessages: string[]
  issueSnippets: string[]
  failureDetails: Array<{
    title: string
    method?: string
    url?: string
    status?: string
    message?: string
  }>
  status: 'ok' | 'partial' | 'failed'
}

type ParsedTestingRun = {
  setupSpecs: string[]
  targetSpec: string | null
  command: string | null
  failureHeadlines: string[]
  specRuns: ParsedTestingSpec[]
  summary:
    | {
        failedSpecs: number
        totalSpecs: number
        failureRateLabel: string
        duration: string
        totalTests: number
        passing: number
        failing: number
      }
    | null
  postRunSteps: string[]
}

function toCount(value: string | undefined): number {
  if (!value || value.trim() === '-') return 0
  const parsed = Number.parseInt(value.trim(), 10)
  return Number.isNaN(parsed) ? 0 : parsed
}

function parseQuotedSpecList(line: string | undefined): string[] {
  if (!line) return []
  return Array.from(line.matchAll(/'([^']+\.cy\.js)'/g), (match) => match[1])
}

function formatDurationAsMs(duration: string | undefined): string {
  if (!duration) return '—'
  const value = duration.trim()

  if (/^\d+ms$/i.test(value)) {
    return value.toLowerCase()
  }

  if (/^\d+s$/i.test(value)) {
    const seconds = Number.parseInt(value, 10)
    return Number.isNaN(seconds) ? value : `${seconds * 1000}ms`
  }

  if (/^\d+\s+second(s)?$/i.test(value)) {
    const seconds = Number.parseInt(value, 10)
    return Number.isNaN(seconds) ? value : `${seconds * 1000}ms`
  }

  if (/^\d{2}:\d{2}$/.test(value)) {
    const [minutes, seconds] = value.split(':').map((part) => Number.parseInt(part, 10))
    if (!Number.isNaN(minutes) && !Number.isNaN(seconds)) {
      return `${(minutes * 60 + seconds) * 1000}ms`
    }
  }

  return value
}

function parseTestingTerminalSummary(text: string | undefined): ParsedTestingRun | null {
  if (!text) return null

  const setupLine = text.match(/setup:\s*\[(.+?)\]/)
  const targetSpec = text.match(/test:\s+([^\n]+\.cy\.js)/)?.[1]?.trim() ?? null
  const command = text.match(/Running:\s+(npx cypress run[^\n]+)/)?.[1]?.trim() ?? null

  const failureHeadlinesBlock = text.match(/Failed tests:\n([\s\S]*?)\n\n[─-]{20,}/)
  const failureHeadlines = failureHeadlinesBlock
    ? Array.from(
        failureHeadlinesBlock[1].matchAll(/^\s*✗\s+(.+)$/gm),
        (match) => match[1].trim(),
      )
    : []

  const summaryTableDurations = new Map<string, string>()
  text.split('\n').forEach((line) => {
    const match = line.match(
      /[│|]\s*[✖✔]\s+([^\s]+\.cy\.js)\s+([0-9:]+|[0-9]+ms)\s+\d+\s+[0-9-]+\s+[0-9-]+\s+[0-9-]+\s+-\s+-\s*[│|]?/,
    )
    if (match) {
      summaryTableDurations.set(match[1].trim(), match[2].trim())
    }
  })

  const specRuns = Array.from(
    text.matchAll(
      /Running:\s+([^\s]+\.cy\.js)[\s\S]*?\(Results\)[\s\S]*?Tests:\s+([0-9-]+)[\s\S]*?Passing:\s+([0-9-]+)[\s\S]*?Failing:\s+([0-9-]+)[\s\S]*?Duration:\s+([^\n]+)[\s\S]*?Spec Ran:\s+([^\n]+)[\s\S]*?(?=\n────────────────|\nStart generate report process|\n====================================================================================================|\s*$)/g,
    ),
  ).map((match) => {
    const block = match[0]
    const tests = toCount(match[2])
    const passing = toCount(match[3])
    const failing = toCount(match[4])
    const pending = toCount(block.match(/Pending:\s+([0-9-]+)/)?.[1])
    const skipped = toCount(block.match(/Skipped:\s+([0-9-]+)/)?.[1])
    const screenshots = toCount(block.match(/Screenshots:\s+([0-9-]+)/)?.[1])
    const videoRaw = block.match(/Video:\s+([^\n]+)/)?.[1]?.trim().toLowerCase()
    const videoEnabled =
      videoRaw == null ? null : videoRaw === 'true' ? true : videoRaw === 'false' ? false : null
    const videoOutput = block.match(/Video output:\s+([^\n]+)/)?.[1]?.trim() ?? null
    const suiteTitles = Array.from(
      new Set(
        block
          .split('\n')
          .map((line) => line.trim())
          .filter(
            (line) =>
              line.length > 0 &&
              !/^[=\-─│┌└├┐┘┤┬┴┼\s]+$/.test(line) &&
              !/^[│\s]*(Tests|Passing|Failing|Pending|Skipped|Screenshots|Video|Duration|Spec Ran):/i.test(
                line,
              ) &&
              !line.startsWith('Running:') &&
              !line.startsWith('Logging console message from task') &&
              !line.startsWith('x-request-id') &&
              !/^\d+\s+(passing|failing|pending)/i.test(line) &&
              !/^[0-9]+\)/.test(line) &&
              !line.startsWith('✓') &&
              !line.startsWith('- ') &&
              !line.startsWith('STEP:') &&
              !line.startsWith('(') &&
              !line.startsWith('[') &&
              !line.startsWith('Method:') &&
              !line.startsWith('URL:') &&
              !line.startsWith('Headers:') &&
              !line.startsWith('Body:') &&
              !line.startsWith('Status:') &&
              !line.startsWith('Error:') &&
              !line.startsWith('CypressError:') &&
              !line.startsWith('Common situations') &&
              !line.startsWith('The request we sent was:') &&
              !line.startsWith('The response we got was:') &&
              !line.startsWith('This was considered a failure') &&
              !line.startsWith('If you do not want') &&
              !line.startsWith('From Your Spec Code:') &&
              !line.startsWith('From Node.js Internals:') &&
              !line.startsWith('at ') &&
              !line.startsWith('>')
          ),
      ),
    ).slice(0, 3)
    const passingNotes = Array.from(
      new Set(
        Array.from(block.matchAll(/^\s*✓\s+(.+)$/gm), (m) => m[1].trim()).filter(
          (line) => !line.includes('create-shadow-config-if-shadow-mode-enabled'),
        ),
      ),
    ).slice(0, 5)
    const requestIds = Array.from(
      new Set(
        Array.from(block.matchAll(/x-request-id\s*->\s*([A-Za-z0-9-]+)/g), (requestIdMatch) => requestIdMatch[1]),
      ),
    )
    const methods = Array.from(
      new Set(Array.from(block.matchAll(/Method:\s+([A-Z]+)/g), (m) => m[1].trim())),
    )
    const urls = Array.from(
      new Set(Array.from(block.matchAll(/URL:\s+(https?:\/\/[^\s]+)/g), (m) => m[1].trim())),
    )
    const responseStatuses = Array.from(
      new Set(Array.from(block.matchAll(/Status:\s+(\d+\s+-\s+[^\n]+)/g), (m) => m[1].trim())),
    )
    const responseMessages = Array.from(
      new Set(
        [
          ...Array.from(block.matchAll(/"message":\s*"([^"]+)"/g), (m) => m[1].trim()),
          ...Array.from(block.matchAll(/Error:\s+([^\n]+)/g), (m) => m[1].trim()),
        ],
      ),
    ).slice(0, 4)
    const inlineDurationMatch = block.match(/\n\s*\d+\s+(?:passing|failing)[^\n]*\((\d+ms|\d+s)\)/i)
    const stepNames = Array.from(
      new Set(
        Array.from(block.matchAll(/STEP:\s+([^\n]+)/g), (stepMatch) => stepMatch[1].trim()),
      ),
    )
    const failedTests = Array.from(
      block.matchAll(/^\s+\d+\)\s+([\s\S]*?):$/gm),
      (failureMatch) => failureMatch[1].replace(/\s+/g, ' ').trim(),
    )
    const issueSnippets = Array.from(
      new Set(
        [
          ...Array.from(block.matchAll(/CypressError:\s*`cy\.request\(\)` failed[^\n]*/g), (m) =>
            m[0].trim(),
          ),
          ...Array.from(block.matchAll(/Error:\s+[^\n]+/g), (m) => m[0].trim()),
          ...Array.from(block.matchAll(/>\s+\d{3}:\s+[^\n]+/g), (m) => m[0].replace(/^>\s+/, '').trim()),
          ...Array.from(block.matchAll(/connect ECONNREFUSED[^\n]*/g), (m) => m[0].trim()),
        ],
      ),
    ).slice(0, 4)
    const failureDetails = Array.from(
      block.matchAll(
        /\n\s+\d+\)\s+([\s\S]*?):\n\s+(CypressError:[\s\S]*?|Error:[\s\S]*?)(?=\n\s+\d+\)\s+|\n\n\[mochawesome\]|\n\n  \(Results\)|$)/g,
      ),
    )
      .map((failureMatch) => {
        const detailBlock = failureMatch[2]
        const message =
          detailBlock.match(/Error:\s+([^\n]+)/)?.[1]?.trim() ??
          detailBlock.match(/CypressError:\s*([^\n]+)/)?.[1]?.trim()
        return {
          title: failureMatch[1].replace(/\s+/g, ' ').trim(),
          method: detailBlock.match(/Method:\s+([A-Z]+)/)?.[1]?.trim(),
          url: detailBlock.match(/URL:\s+(https?:\/\/[^\s]+)/)?.[1]?.trim(),
          status:
            detailBlock.match(/Status:\s+(\d+\s+-\s+[^\n]+)/)?.[1]?.trim() ??
            detailBlock.match(/connect ECONNREFUSED[^\n]*/)?.[0]?.trim(),
          message,
        }
      })
      .slice(0, 6)

    const specName = match[6].trim() || match[1].trim()

    return {
      spec: specName,
      tests,
      passing,
      failing,
      pending,
      skipped,
      screenshots,
      videoEnabled,
      videoOutput,
      duration: formatDurationAsMs(
        inlineDurationMatch?.[1] ?? summaryTableDurations.get(specName) ?? match[5].trim(),
      ),
      suiteTitles,
      passingNotes,
      failedTests,
      requestIds,
      stepNames,
      methods,
      urls,
      responseStatuses,
      responseMessages,
      issueSnippets,
      failureDetails,
      status: failing > 0 ? 'failed' : passing > 0 ? 'ok' : 'partial',
    } satisfies ParsedTestingSpec
  })

  const summaryMatch = text.match(
    /✖\s+(\d+)\s+of\s+(\d+)\s+failed\s+\(([^)]+)\)\s+([0-9:]+)\s+(\d+)\s+(\d+)\s+(\d+)/,
  )

  const postRunSteps = [
    ...Array.from(
      text.matchAll(/^(Start generate report process|Read and merge jsons[^\n]*|Copy media folder[^\n]*|Enhance report|Create HTML report|HTML report successfully created!|Auto-fix: [^\n]*|Simple value fix failed[^\n]*|LLM response missing [^\n]*|❌ [^\n]*|Flow pipeline completed)$/gm),
      (match) => match[1].trim(),
    ),
  ]

  return {
    setupSpecs: parseQuotedSpecList(setupLine?.[0]),
    targetSpec,
    command,
    failureHeadlines,
    specRuns,
    summary: summaryMatch
      ? {
          failedSpecs: toCount(summaryMatch[1]),
          totalSpecs: toCount(summaryMatch[2]),
          failureRateLabel: summaryMatch[3].trim(),
          duration: formatDurationAsMs(summaryMatch[4].trim()),
          totalTests: toCount(summaryMatch[5]),
          passing: toCount(summaryMatch[6]),
          failing: toCount(summaryMatch[7]),
        }
      : null,
    postRunSteps,
  }
}

export default function App() {
  const { theme, toggle } = useTheme()
  const [run, setRun] = useState<LoadedRun | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [runs, setRuns] = useState<string[]>([])
  const [runsLoading, setRunsLoading] = useState(true)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<RunTab>('overview')
  const [copiedCommand, setCopiedCommand] = useState(false)

  const refreshRuns = useCallback(async () => {
    setRunsLoading(true)
    setRunsError(null)
    try {
      const ids = await fetchOutputRunIds()
      setRuns(ids)
    } catch (e) {
      setRuns([])
      setRunsError(e instanceof Error ? e.message : 'Failed to list runs')
    } finally {
      setRunsLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshRuns()
  }, [refreshRuns])

  const selectOutputRun = useCallback(async (runId: string) => {
    setLoading(true)
    setLoadError(null)
    try {
      const r = await loadRunFromOutputFolder(runId)
      setActiveRunId(runId)
      setRun(r)
      setActiveTab('overview')
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Failed to load run')
    } finally {
      setLoading(false)
    }
  }, [])

  const summary = useMemo(() => {
    if (!run?.finalReport) return null
    const fr = run.finalReport
    return {
      requestId: fr.request_id,
      endpoint: fr.api_call?.endpoint,
      status: fr.api_call?.http_status_code,
      error: fr.api_call?.error,
      leaf: fr.coverage_diff?.leaf,
      rootCause: fr.root_cause_analysis,
    }
  }, [run])

  const coverageHtmlUrl = useMemo(() => {
    if (!run?.id) return null
    return `/api/run-html/${encodeURIComponent(run.id)}/index.html`
  }, [run?.id])

  const cypressParsed = useMemo(
    () => parseJsonObject(run?.rawFiles['cypress_parsed.json']) as
      | {
          request_ids?: string[]
          passing_count?: number
          failing_count?: number
          total_tests?: number
          failed_test_names?: string[]
          errors?: string[]
          test_passed?: boolean
        }
      | undefined,
    [run],
  )

  const testingRun = useMemo(
    () =>
      parseTestingTerminalSummary(
        run?.rawFiles['terminal_output.log'] ?? run?.rawFiles['flow_pipeline.log'],
      ),
    [run],
  )

  const overviewCoverageBlock = useMemo(() => {
    if (!run) return null
    const blocks = buildPathFlowCoverageBlocks(
      run.pathFlow,
      run.coverageReport,
      run.finalReport,
      run.rawFiles['line_hits.txt'],
      run.rawFiles['lcov.info'],
    )
    return blocks.find((block) => block.roleLabel === 'Leaf') ?? blocks[blocks.length - 1] ?? null
  }, [run])

  const overviewCoverageSummary = useMemo(() => {
    if (!run) return null
    const blocks = buildPathFlowCoverageBlocks(
      run.pathFlow,
      run.coverageReport,
      run.finalReport,
      run.rawFiles['line_hits.txt'],
      run.rawFiles['lcov.info'],
    )
    const measurableLines = blocks.flatMap((block) => block.lines).filter((line) => line.hits !== null)
    const coveredLines = measurableLines.filter((line) => (line.hits ?? 0) > 0)
    const missedLines = measurableLines.filter((line) => line.hits === 0)
    const fullyCoveredFunctions = blocks.filter((block) => {
      const measured = block.lines.filter((line) => line.hits !== null)
      return measured.length > 0 && measured.every((line) => (line.hits ?? 0) > 0)
    }).length

    return {
      functionCount: blocks.length,
      fullyCoveredFunctions,
      measurableLineCount: measurableLines.length,
      coveredLineCount: coveredLines.length,
      missedLineCount: missedLines.length,
      percentage: measurableLines.length
        ? (coveredLines.length / measurableLines.length) * 100
        : null,
    }
  }, [run])

  const overview = useMemo(() => {
    if (!run) return null

    const fallbackRequestIds = run.finalReport?.request_id
      ? [run.finalReport.request_id]
      : []
    const requestIds =
      cypressParsed?.request_ids?.length ? cypressParsed.request_ids : fallbackRequestIds
    const passingCount =
      cypressParsed?.passing_count ?? run.finalReport?.test_results?.passing_count ?? 0
    const failingCount =
      cypressParsed?.failing_count ??
      run.finalReport?.test_results?.failing_count ??
      run.finalReport?.api_call?.error?.failing_count ??
      0
    const totalTests =
      cypressParsed?.total_tests ?? run.finalReport?.test_results?.total_tests ?? 0
    const errors = [
      ...(cypressParsed?.errors ?? []),
      ...(run.finalReport?.api_call?.error?.errors ?? []),
      run.finalReport?.root_cause_analysis?.reason ?? '',
    ].filter(Boolean)
    const failedTests = [
      ...(cypressParsed?.failed_test_names ?? []),
      ...(run.finalReport?.api_call?.error?.failed_tests ?? []),
    ].filter(Boolean)
    const profrawAvailable = run.coverageReport?.d?.kind !== 'coverage_unavailable'
    const apiStatus = run.finalReport?.api_call?.http_status_code
    const apiError = run.finalReport?.api_call?.error
    const rca = run.finalReport?.root_cause_analysis
    const coverageRatio = run.finalReport?.coverage_diff?.line_coverage_ratio
    const leaf = run.finalReport?.coverage_diff?.leaf
    const routerLogAvailable =
      Boolean(run.rawFiles['router_run.log']) ||
      Boolean(run.finalReport?.router_log_correlation?.router_log_path) ||
      (run.finalReport?.router_log_correlation?.matches_in_log ?? 0) > 0

    const checkpoints: Array<{
      label: string
      status: 'ok' | 'partial' | 'failed'
      detail: string
    }> = [
      {
        label: 'Router',
        status: routerLogAvailable ? 'ok' : 'failed',
        detail: routerLogAvailable
          ? 'Router activity was captured for this run.'
          : 'Router log details were not found for this run.',
      },
      {
        label: 'Testing',
        status:
          totalTests === 0 ? 'failed' : failingCount > 0 ? 'partial' : 'ok',
        detail:
          totalTests === 0
            ? 'No Cypress tests were parsed from the run.'
            : `${passingCount} passed, ${failingCount} failed, ${Math.max(totalTests - passingCount - failingCount, 0)} remaining/pending.`,
      },
      {
        label: 'Coverage',
        status: profrawAvailable ? 'ok' : 'partial',
        detail: profrawAvailable
          ? 'LLVM profile data was collected and coverage artifacts were generated.'
          : 'Coverage artifacts are partial because no LLVM profile data was found.',
      },
      {
        label: 'Final report',
        status: run.finalReport ? 'ok' : 'failed',
        detail: run.finalReport
          ? 'Final RCA report was generated for this run.'
          : 'Final RCA report is missing.',
      },
    ]

    const coverageTarget =
      leaf?.name || shortFileName(leaf?.file) || undefined

    const routeMismatch =
      errors.some((error) => includesAny(error, ['/accounts', 'unrecognized request url'])) ||
      failedTests.some((test) => includesAny(test, ['account create']))
    const connectorSetupFailure =
      errors.some((error) => includesAny(error, ['connector create call failed'])) ||
      failedTests.some((test) => includesAny(test, ['connector account create']))
    const paymentFlowFailure =
      errors.some((error) =>
        includesAny(error, ['client_secret', 'expecting valid response']),
      ) ||
      failedTests.some((test) =>
        includesAny(test, ['create payment intent', 'confirm payment intent', 'capture payment intent']),
      )

    const whatWorked = [
      routerLogAvailable ? 'router activity was recorded' : null,
      run.rawFiles['flow_pipeline.log'] ? 'flow pipeline ran' : null,
      totalTests > 0 ? `Cypress executed ${totalTests} parsed checks` : null,
      requestIds.length ? 'request IDs were captured' : null,
      run.rawFiles['cypress_parsed.json'] ? 'Cypress results were parsed' : null,
      run.finalReport ? 'final report was generated' : null,
    ].filter(Boolean) as string[]

    const specBreakdown = [
      {
        spec: '00001-AccountCreate.cy.js',
        summary: 'merchant create and API key create are failing',
        show:
          failedTests.some((test) => includesAny(test, ['account create'])) ||
          errors.some((error) => includesAny(error, ['/accounts', 'api key create'])),
      },
      {
        spec: '00002-CustomerCreate.cy.js',
        summary: 'customer create is passing, so the environment is not universally broken',
        show: run.rawFiles['flow_pipeline.log']?.includes('00002-CustomerCreate.cy.js') ?? false,
      },
      {
        spec: '00003-ConnectorCreate.cy.js',
        summary: connectorSetupFailure
          ? 'connector create reaches the API, but the backend route/API still does not match what the test expects'
          : 'connector setup did not show a clear failure in this run',
        show:
          run.rawFiles['flow_pipeline.log']?.includes('00003-ConnectorCreate.cy.js') ?? false,
      },
      {
        spec: '00029-IncrementalAuth.cy.js',
        summary: paymentFlowFailure
          ? 'the target flow is reached, but payment create / confirm / capture still fail'
          : 'the target payment flow did not show a clear failure in this run',
        show:
          run.rawFiles['flow_pipeline.log']?.includes('00029-IncrementalAuth.cy.js') ?? false,
      },
    ].filter((item) => item.show)

    const narrative = [
      coverageRatio !== undefined
        ? `Current coverage for the focused area is ${coverageRatio}.${coverageTarget ? ` The target function is ${coverageTarget}.` : ''}`
        : profrawAvailable
          ? coverageTarget
            ? `Coverage artifacts were generated for this run. The target function is ${coverageTarget}.`
            : 'Coverage artifacts were generated for this run.'
          : 'Coverage is partial for this run because no LLVM profile files were collected.',
      routeMismatch
        ? 'The first visible blocker is the account setup flow: the backend is returning "Unrecognized request URL" for /accounts, so merchant and API key setup do not complete successfully.'
        : null,
      connectorSetupFailure
        ? 'Connector setup is also failing later in the run, which means the connector account is not being created in a usable state.'
        : null,
      paymentFlowFailure
        ? 'The payment flow reaches incremental authorization, but payment create / confirm / capture still fail because earlier setup and API responses are not in the expected state.'
        : null,
      !routeMismatch && !connectorSetupFailure && !paymentFlowFailure && rca?.reason
        ? rca.reason
        : null,
      totalTests
        ? `Overall, Cypress parsed ${totalTests} checks for this run with ${passingCount} passing and ${failingCount} failing.`
        : null,
      requestIds.length
        ? `The run captured ${requestIds.length} request id${requestIds.length === 1 ? '' : 's'}, so logs and failures can be correlated in the report.`
        : null,
    ].filter(Boolean) as string[]

    return {
      checkpoints,
      highlights: narrative,
      apiStatus,
      apiError,
      whatWorked,
      routeMismatch,
      connectorSetupFailure,
      paymentFlowFailure,
      requestIds,
      specBreakdown,
      profrawAvailable,
    }
  }, [run, cypressParsed])

  const overviewRequestId = overview?.requestIds?.[0] ?? summary?.requestId ?? null

  return (
    <div className="flex min-h-dvh flex-col bg-[var(--app-bg)] text-[var(--app-text)]">
      <header
        className="sticky top-0 z-20 border-b border-[var(--app-border)] backdrop-blur-md"
        style={{ background: 'var(--app-header-bg)' }}
      >
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-4 px-5 py-4">
          <div className="flex items-center gap-3">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-[var(--app-accent)]">
                Hyperswitch
              </p>
              <h1 className="text-lg font-semibold tracking-tight text-[var(--app-text)]">
                Coverage flow explorer
              </h1>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={toggle}
              className="inline-flex items-center gap-2 rounded-xl border border-[var(--app-border)] bg-[var(--app-surface)] px-3 py-2 text-sm text-[var(--app-text-secondary)] transition hover:border-[color-mix(in_oklab,var(--app-accent)_40%,var(--app-border))]"
              aria-label={
                theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'
              }
            >
              {theme === 'dark' ? (
                <Sun className="size-4 text-amber-400" aria-hidden />
              ) : (
                <Moon className="size-4 text-slate-600" aria-hidden />
              )}
              <span className="hidden sm:inline">
                {theme === 'dark' ? 'Light' : 'Dark'}
              </span>
            </button>
          </div>
        </div>
        {loadError ? (
          <p
            className={`border-t border-red-500/20 bg-red-500/10 px-5 py-2 text-center text-sm ${
              theme === 'light' ? 'text-red-700' : 'text-red-200'
            }`}
          >
            {loadError}
          </p>
        ) : null}
      </header>

      <main className="mx-auto flex w-full max-w-[1600px] flex-1 flex-col gap-8 px-5 py-6">
        <section className="flex flex-col gap-5">
          {run ? (
            <>
              <div className="min-w-0 space-y-0">
                <h2 className="text-lg font-semibold tracking-tight text-[var(--app-text)] sm:text-xl">
                  Architecture & flow
                </h2>
                <FlowBreadcrumb
                  runId={run.id}
                  onNavigateToRuns={() => setRun(null)}
                />
              </div>
              <div className="h-[520px] w-full min-h-[420px]">
                <FlowCanvas
                  pathFlow={run.pathFlow}
                  finalReport={run.finalReport}
                  reverse={false}
                />
              </div>
            </>
          ) : (
            <div className="h-[520px] w-full">
              <RunPickerPanel
                theme={theme}
                runs={runs}
                loadingList={runsLoading}
                loadingRun={loading}
                listError={runsError}
                selectedId={activeRunId}
                onSelect={(id) => void selectOutputRun(id)}
                onRefreshList={() => void refreshRuns()}
              />
            </div>
          )}
        </section>

        {run ? (
          <div className="flex flex-col gap-3">
            <div className="inline-flex w-fit rounded-xl border border-[var(--app-border)] bg-[var(--app-surface)] p-1">
              {(
                [
                  ['overview', 'Overview'],
                  ['testing', 'Testing'],
                  ['coverage', 'Coverage'],
                  ['artifacts', 'Logs'],
                ] as [RunTab, string][]
              ).map(([tab, label]) => (
                <button
                  key={tab}
                  type="button"
                  onClick={() => setActiveTab(tab)}
                  className={`rounded-lg px-3 py-1.5 text-sm transition ${
                    activeTab === tab
                      ? 'bg-[var(--app-accent-muted)] text-[var(--app-accent)]'
                      : 'text-[var(--app-text-secondary)] hover:bg-[var(--app-elevated)]'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            {activeTab === 'overview' ? (
              <>
                {run.finalReport ? (
                  <CollapsibleSection title="Run summary" defaultOpen>
                    <div className="space-y-4">
                      <div className="grid gap-4 text-sm md:grid-cols-2">
                        <div className="min-w-0">
                          <p className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                            Run id
                          </p>
                          <p
                            className="mt-1 break-all font-mono text-xs text-[var(--app-text)]"
                            title={run.id}
                          >
                            {run.id}
                          </p>
                        </div>
                        {overviewRequestId ? (
                          <div className="min-w-0">
                            <p className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                              Request id
                            </p>
                            <p
                              className="mt-1 break-all font-mono text-xs text-[var(--app-text-secondary)] [overflow-wrap:anywhere]"
                              title={overviewRequestId}
                            >
                              {overviewRequestId}
                            </p>
                          </div>
                        ) : null}
                      </div>

                      {overview ? (
                        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                          {overview.checkpoints.map((item) => (
                            <div
                              key={item.label}
                              className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-3"
                            >
                              <div className="flex items-start justify-between gap-2">
                                <p className="text-xs uppercase tracking-wider text-[var(--app-muted)]">
                                  {item.label}
                                </p>
                                <span
                                  className={`rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ${statusTone(item.status)}`}
                                >
                                  {item.status}
                                </span>
                              </div>
                              <p className="mt-2 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                                {item.detail}
                              </p>
                            </div>
                          ))}
                        </div>
                      ) : null}

                      {testingRun?.summary || overviewCoverageSummary || run.finalReport?.root_cause_analysis ? (
                        <div className="grid gap-4">
                          <div
                            className={
                              testingRun?.summary ? 'grid gap-4' : 'grid gap-4 xl:grid-cols-2'
                            }
                          >
                            {overviewCoverageSummary ? (
                              <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
                                <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                                  Coverage summary
                                </h3>
                                <div className="mt-4 grid gap-3 sm:grid-cols-4">
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-3">
                                    <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                                      Flow coverage
                                    </p>
                                    <p className="mt-2 text-lg font-semibold text-[var(--app-text)]">
                                      {overviewCoverageSummary.percentage == null
                                        ? '—'
                                        : `${overviewCoverageSummary.percentage.toFixed(1)}%`}
                                    </p>
                                  </div>
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-3">
                                    <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                                      Functions
                                    </p>
                                    <p className="mt-2 text-lg font-semibold text-[var(--app-text)]">
                                      {overviewCoverageSummary.functionCount}
                                    </p>
                                  </div>
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-3">
                                    <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                                      Covered lines
                                    </p>
                                    <p className="mt-2 text-lg font-semibold text-emerald-500">
                                      {overviewCoverageSummary.coveredLineCount}
                                    </p>
                                  </div>
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-3">
                                    <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                                      Missed lines
                                    </p>
                                    <p className="mt-2 text-lg font-semibold text-rose-500">
                                      {overviewCoverageSummary.missedLineCount}
                                    </p>
                                  </div>
                                </div>
                                <p className="mt-3 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                                  {overviewCoverageSummary.fullyCoveredFunctions} fully covered function{overviewCoverageSummary.fullyCoveredFunctions === 1 ? '' : 's'} out of {overviewCoverageSummary.functionCount} on the selected flow path.
                                </p>
                              </div>
                            ) : null}

                            {run.finalReport?.root_cause_analysis ? (
                              <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
                                <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                                  Root cause analysis
                                </h3>
                                <div className="mt-4 grid gap-4 xl:grid-cols-[260px_minmax(0,1fr)]">
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                                    <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                                      Status
                                    </p>
                                    <div className="mt-3">
                                      <span className="inline-flex rounded-full border border-amber-500/20 bg-amber-500/10 px-3 py-1 text-sm font-medium capitalize tracking-wide text-amber-400">
                                        {run.finalReport.root_cause_analysis.status?.replace(/_/g, ' ') ?? 'Unknown'}
                                      </span>
                                    </div>
                                  </div>
                                  <div className="grid gap-3">
                                    {run.finalReport.root_cause_analysis.reason ? (
                                      <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                                        <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                                          Main reason
                                        </p>
                                        <p className="mt-2 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                                          {run.finalReport.root_cause_analysis.reason}
                                        </p>
                                      </div>
                                    ) : null}
                                    {run.finalReport.root_cause_analysis.why_coverage_is_zero ? (
                                      <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                                        <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                                          Coverage impact
                                        </p>
                                        <p className="mt-2 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                                          {run.finalReport.root_cause_analysis.why_coverage_is_zero}
                                        </p>
                                      </div>
                                    ) : null}
                                    {run.finalReport.root_cause_analysis.contributing_failures?.length ? (
                                      <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                                        <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                                          Related failures
                                        </p>
                                        <div className="mt-2 space-y-2">
                                          {run.finalReport.root_cause_analysis.contributing_failures.map((item) => (
                                            <div
                                              key={item}
                                              className="rounded-md border border-[var(--app-border)] bg-[var(--app-elevated)] px-3 py-2 text-sm leading-relaxed text-[var(--app-text-secondary)]"
                                            >
                                              {item}
                                            </div>
                                          ))}
                                        </div>
                                      </div>
                                    ) : null}
                                  </div>
                                </div>
                              </div>
                            ) : null}
                          </div>
                        </div>
                      ) : null}

                      {overview ? (
                        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
                          <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                            What happened overall
                          </h3>
                          <div className="mt-3 space-y-2">
                            {overview.whatWorked.map((item) => (
                              <div
                                key={item}
                                className="flex items-center gap-3 rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] px-3 py-2"
                              >
                                <span className="inline-flex size-5 items-center justify-center rounded-full bg-emerald-500/10 text-xs font-semibold text-emerald-400">
                                  ✓
                                </span>
                                <p className="text-sm leading-relaxed text-[var(--app-text-secondary)]">
                                  {item}
                                </p>
                              </div>
                            ))}
                          </div>
                          <p className="mt-4 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                            So this is not a pipeline failure. It is a successful run of the
                            pipeline with failing business or test steps.
                          </p>
                        </div>
                      ) : null}

                      {overview?.highlights?.length ? (
                        <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-elevated)] p-4">
                          <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                            Main things this run tells us
                          </h3>
                          <ol className="mt-3 space-y-3 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                            {overview.highlights.map((item, index) => (
                              <li key={item}>
                                <span className="font-semibold text-[var(--app-text)]">
                                  {index + 1}.
                                </span>{' '}
                                {item}
                              </li>
                            ))}
                          </ol>
                        </div>
                      ) : null}

                    </div>
                  </CollapsibleSection>
                ) : (
                  <CollapsibleSection title="Run summary" defaultOpen>
                    <div className="grid gap-4 text-sm md:grid-cols-2">
                      <div className="min-w-0">
                        <p className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                          Run id
                        </p>
                        <p
                          className="mt-1 break-all font-mono text-xs text-[var(--app-text)]"
                          title={run.id}
                        >
                          {run.id}
                        </p>
                      </div>
                      {overviewRequestId ? (
                        <div className="min-w-0">
                          <p className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                            Request id
                          </p>
                          <p
                            className="mt-1 break-all font-mono text-xs text-[var(--app-text-secondary)] [overflow-wrap:anywhere]"
                            title={overviewRequestId}
                          >
                            {overviewRequestId}
                          </p>
                        </div>
                      ) : null}
                    </div>
                  </CollapsibleSection>
                )}
              </>
            ) : null}

            {activeTab === 'testing' ? (
              <CollapsibleSection title="Testing pipeline" defaultOpen>
                <div className="space-y-6 text-sm">
                  <div className="grid gap-4 lg:grid-cols-4">
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                      <p className="text-xs uppercase tracking-wider text-[var(--app-muted)]">
                        Overall result
                      </p>
                      <p className="mt-3 text-lg font-semibold text-[var(--app-text)]">
                        {(overview?.apiStatus ?? 'partial') === 'ok'
                          ? 'Tests passed'
                          : (overview?.apiStatus ?? 'partial') === 'failed'
                            ? 'Tests failed'
                            : 'Partially successful'}
                      </p>
                      <p className="mt-2 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                        {(cypressParsed?.total_tests ?? run.finalReport?.test_results?.total_tests ?? 0) > 0
                          ? `${cypressParsed?.passing_count ?? run.finalReport?.test_results?.passing_count ?? 0} passed and ${cypressParsed?.failing_count ?? run.finalReport?.test_results?.failing_count ?? 0} failed in the parsed test output.`
                          : 'No parsed Cypress test count is available for this run.'}
                      </p>
                    </div>
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                      <p className="text-xs uppercase tracking-wider text-[var(--app-muted)]">
                        Passed checks
                      </p>
                      <p className="mt-3 text-2xl font-semibold text-emerald-500">
                        {cypressParsed?.passing_count ?? run.finalReport?.test_results?.passing_count ?? 0}
                      </p>
                      <p className="mt-2 text-sm text-[var(--app-text-secondary)]">
                        Parsed passing Cypress checks.
                      </p>
                    </div>
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                      <p className="text-xs uppercase tracking-wider text-[var(--app-muted)]">
                        Failed checks
                      </p>
                      <p className="mt-3 text-2xl font-semibold text-rose-500">
                        {cypressParsed?.failing_count ??
                          run.finalReport?.test_results?.failing_count ??
                          0}
                      </p>
                      <p className="mt-2 text-sm text-[var(--app-text-secondary)]">
                        Parsed failing Cypress checks.
                      </p>
                    </div>
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                      <p className="text-xs uppercase tracking-wider text-[var(--app-muted)]">
                        Total parsed
                      </p>
                      <p className="mt-3 text-2xl font-semibold text-[var(--app-text)]">
                        {cypressParsed?.total_tests ?? run.finalReport?.test_results?.total_tests ?? 0}
                      </p>
                      <p className="mt-2 text-sm text-[var(--app-text-secondary)]">
                        Total checks seen by the parser for this run.
                      </p>
                    </div>
                  </div>

                  {(testingRun?.setupSpecs.length || testingRun?.targetSpec || testingRun?.command) ? (
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                      <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                        Cypress execution plan
                      </h3>
                      <div className="mt-4 grid gap-4 lg:grid-cols-3">
                        <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-elevated)] p-3">
                          <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                            Setup specs
                          </p>
                          {testingRun?.setupSpecs.length ? (
                            <ul className="mt-3 space-y-2 text-sm text-[var(--app-text-secondary)]">
                              {testingRun.setupSpecs.map((spec) => (
                                <li key={spec} className="font-mono text-xs">
                                  {spec}
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <p className="mt-3 text-sm text-[var(--app-muted)]">
                              Setup specs were not detected in the terminal output.
                            </p>
                          )}
                        </div>
                        <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-elevated)] p-3">
                          <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                            Target spec
                          </p>
                          <p className="mt-3 font-mono text-sm text-[var(--app-text)]">
                            {testingRun?.targetSpec ?? 'Not detected'}
                          </p>
                        </div>
                        <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-elevated)] p-3">
                          <div className="flex items-center justify-between gap-3">
                            <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                              Cypress command
                            </p>
                            {testingRun?.command ? (
                              <button
                                type="button"
                                onClick={() => {
                                  void navigator.clipboard.writeText(testingRun.command ?? '')
                                  setCopiedCommand(true)
                                  window.setTimeout(() => setCopiedCommand(false), 1500)
                                }}
                                className="rounded-md border border-[var(--app-border)] bg-[var(--app-card)] px-2 py-1 text-[11px] font-medium text-[var(--app-text-secondary)] transition hover:border-[var(--app-accent)] hover:text-[var(--app-text)]"
                              >
                                {copiedCommand ? 'Copied' : 'Copy'}
                              </button>
                            ) : null}
                          </div>
                          {testingRun?.command ? (
                            <pre className="mt-3 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-[var(--app-border)] bg-[var(--app-code-bg)] p-3 font-mono text-xs leading-relaxed text-[var(--app-text-secondary)]">
                              <code>{testingRun.command}</code>
                            </pre>
                          ) : (
                            <p className="mt-3 text-sm text-[var(--app-muted)]">
                              The Cypress command was not found in the terminal output.
                            </p>
                          )}
                        </div>
                      </div>
                    </div>
                  ) : null}

                  {testingRun?.summary ? (
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                      <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                        End-to-end test summary
                      </h3>
                      <div className="mt-4 grid gap-4 lg:grid-cols-4">
                        <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-elevated)] p-3">
                          <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                            Spec result
                          </p>
                          <p className="mt-2 text-lg font-semibold text-[var(--app-text)]">
                            {testingRun.summary.failedSpecs} of {testingRun.summary.totalSpecs} failed
                          </p>
                        </div>
                        <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-elevated)] p-3">
                          <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                            Failure rate
                          </p>
                          <p className="mt-2 text-lg font-semibold text-rose-500">
                            {testingRun.summary.failureRateLabel}
                          </p>
                        </div>
                        <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-elevated)] p-3">
                          <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                            Total checks
                          </p>
                          <p className="mt-2 text-lg font-semibold text-[var(--app-text)]">
                            {testingRun.summary.totalTests}
                          </p>
                        </div>
                        <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-elevated)] p-3">
                          <p className="text-[11px] uppercase tracking-wide text-[var(--app-subtle)]">
                            Total duration
                          </p>
                          <p className="mt-2 text-lg font-semibold text-[var(--app-text)]">
                            {testingRun.summary.duration}
                          </p>
                        </div>
                      </div>
                    </div>
                  ) : null}

                  {testingRun?.failureHeadlines.length ? (
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                      <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                        Failed checks seen in terminal output
                      </h3>
                      <div className="mt-3 space-y-2">
                        {testingRun.failureHeadlines.map((item) => (
                          <div
                            key={item}
                            className="rounded-lg border border-rose-500/20 bg-rose-500/8 p-3 text-sm leading-relaxed text-[var(--app-text-secondary)]"
                          >
                            {item}
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {testingRun?.specRuns.length ? (
                    <div className="rounded-xl border border-[var(--app-border)] bg-[var(--app-card)] p-4">
                      <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--app-muted)]">
                        Per-spec execution details
                      </h3>
                      <div className="mt-3 space-y-3">
                        {testingRun.specRuns.map((specRun) => (
                          <details
                            key={`${specRun.spec}-${specRun.duration}`}
                            className="overflow-hidden rounded-lg border border-[var(--app-border)] bg-[var(--app-elevated)] group"
                          >
                            <summary className="list-none cursor-pointer p-4 transition hover:bg-[var(--app-card)]">
                              <div className="flex items-start justify-between gap-3">
                                <div className="min-w-0">
                                  <p className="font-mono text-xs text-[var(--app-text)]">
                                    {specRun.spec}
                                  </p>
                                  <div className="mt-3 flex flex-wrap items-center gap-2">
                                    <span
                                      className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${statusTone(specRun.status)}`}
                                    >
                                      {specRun.status}
                                    </span>
                                    <span className="inline-flex rounded-full border border-[var(--app-border)] bg-[var(--app-card)] px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--app-muted)]">
                                      {formatDurationAsMs(specRun.duration)}
                                    </span>
                                  </div>
                                </div>
                                <ChevronDown
                                  className="size-4 shrink-0 text-[var(--app-muted)] transition-transform duration-200 group-open:rotate-180"
                                  aria-hidden
                                />
                              </div>
                              <div className="mt-4 grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] px-3 py-2">
                                    <p className="text-sm font-semibold text-emerald-500">
                                      {specRun.passing}
                                    </p>
                                    <p className="mt-0.5 text-[11px] uppercase tracking-wide text-[var(--app-muted)]">
                                      Passed
                                    </p>
                                  </div>
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] px-3 py-2">
                                    <p className="text-sm font-semibold text-rose-500">{specRun.failing}</p>
                                    <p className="mt-0.5 text-[11px] uppercase tracking-wide text-[var(--app-muted)]">
                                      Failed
                                    </p>
                                  </div>
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] px-3 py-2">
                                    <p className="text-sm font-semibold text-[var(--app-text)]">
                                      {specRun.tests}
                                    </p>
                                    <p className="mt-0.5 text-[11px] uppercase tracking-wide text-[var(--app-muted)]">
                                      Total checks
                                    </p>
                                  </div>
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] px-3 py-2">
                                    <p className="text-sm font-semibold text-[var(--app-text)]">
                                      {specRun.pending}
                                    </p>
                                    <p className="mt-0.5 text-[11px] uppercase tracking-wide text-[var(--app-muted)]">
                                      Pending
                                    </p>
                                  </div>
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] px-3 py-2">
                                    <p className="text-sm font-semibold text-[var(--app-text)]">
                                      {specRun.skipped}
                                    </p>
                                    <p className="mt-0.5 text-[11px] uppercase tracking-wide text-[var(--app-muted)]">
                                      Skipped
                                    </p>
                                  </div>
                                  <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] px-3 py-2">
                                    <p className="text-sm font-semibold text-[var(--app-text)]">
                                      {specRun.screenshots}
                                    </p>
                                    <p className="mt-0.5 text-[11px] uppercase tracking-wide text-[var(--app-muted)]">
                                      Screenshots
                                    </p>
                                  </div>
                              </div>
                            </summary>
                            <div className="border-t border-[var(--app-border)] p-4">
                              <p className="text-sm leading-relaxed text-[var(--app-text-secondary)]">
                                {specRun.failing > 0
                                  ? `${specRun.failing} failure${specRun.failing === 1 ? '' : 's'} were recorded in this spec after ${specRun.passing} successful check${specRun.passing === 1 ? '' : 's'}.`
                                  : `${specRun.passing} check${specRun.passing === 1 ? '' : 's'} completed successfully in this spec.`}
                                {specRun.pending > 0
                                  ? ` ${specRun.pending} check${specRun.pending === 1 ? '' : 's'} remained pending.`
                                  : ''}
                              </p>
                              {specRun.suiteTitles.length ? (
                                <div className="mt-4 rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-3">
                                  <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                                    Test context
                                  </p>
                                  <div className="mt-2 space-y-1 text-sm text-[var(--app-text-secondary)]">
                                    {specRun.suiteTitles.map((title) => (
                                      <p key={title}>{title}</p>
                                    ))}
                                  </div>
                                </div>
                              ) : null}
                            {specRun.passingNotes.length ? (
                              <div className="mt-4 rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-3">
                                <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                                  Successful checks seen in this spec
                                </p>
                                <div className="mt-2 space-y-1 text-sm text-[var(--app-text-secondary)]">
                                  {specRun.passingNotes.map((note) => (
                                    <p key={note}>{note}</p>
                                  ))}
                                </div>
                              </div>
                            ) : null}
                            {(specRun.videoEnabled !== null || specRun.videoOutput) ? (
                              <div className="mt-4 rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-3">
                                <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                                  Media output
                                </p>
                                <div className="mt-2 space-y-2 text-sm text-[var(--app-text-secondary)]">
                                  {specRun.videoEnabled !== null ? (
                                    <p>
                                      <span className="font-medium text-[var(--app-text)]">Video:</span>{' '}
                                      {specRun.videoEnabled ? 'enabled' : 'not generated'}
                                    </p>
                                  ) : null}
                                  {specRun.videoOutput ? (
                                    <p className="break-all">
                                      <span className="font-medium text-[var(--app-text)]">Path:</span>{' '}
                                      {specRun.videoOutput}
                                    </p>
                                  ) : null}
                                </div>
                              </div>
                            ) : null}
                            {(specRun.methods.length ||
                              specRun.urls.length ||
                              specRun.responseStatuses.length ||
                              specRun.responseMessages.length) ? (
                              <div className="mt-4 grid gap-3 lg:grid-cols-2">
                                <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-3">
                                  <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                                    Request details
                                  </p>
                                  <div className="mt-2 space-y-2 text-sm text-[var(--app-text-secondary)]">
                                    {specRun.methods.length ? (
                                      <p>
                                        <span className="font-medium text-[var(--app-text)]">Method:</span>{' '}
                                        {specRun.methods.join(', ')}
                                      </p>
                                    ) : null}
                                    {specRun.urls.length ? (
                                      <p className="break-all">
                                        <span className="font-medium text-[var(--app-text)]">URL:</span>{' '}
                                        {specRun.urls[0]}
                                        {specRun.urls.length > 1 ? ` (+${specRun.urls.length - 1} more)` : ''}
                                      </p>
                                    ) : null}
                                  </div>
                                </div>
                                <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-3">
                                  <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                                    Response details
                                  </p>
                                  <div className="mt-2 space-y-2 text-sm text-[var(--app-text-secondary)]">
                                    {specRun.responseStatuses.length ? (
                                      <p>
                                        <span className="font-medium text-[var(--app-text)]">Status:</span>{' '}
                                        {specRun.responseStatuses.join(', ')}
                                      </p>
                                    ) : null}
                                    {specRun.responseMessages.length ? (
                                      <p className="leading-relaxed">
                                        <span className="font-medium text-[var(--app-text)]">Message:</span>{' '}
                                        {specRun.responseMessages[0]}
                                      </p>
                                    ) : null}
                                  </div>
                                </div>
                              </div>
                            ) : null}
                            {specRun.failureDetails.length ? (
                              <div className="mt-4">
                                <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                                  Failure breakdown
                                </p>
                                <div className="mt-2 space-y-2">
                                  {specRun.failureDetails.map((failure) => (
                                    <div
                                      key={`${failure.title}-${failure.url ?? ''}`}
                                      className="rounded-md border border-rose-500/20 bg-rose-500/8 px-3 py-3"
                                    >
                                      <p className="text-sm font-medium text-[var(--app-text)]">
                                        {failure.title}
                                      </p>
                                      <div className="mt-2 space-y-1 text-sm leading-relaxed text-[var(--app-text-secondary)]">
                                        {failure.method ? (
                                          <p>
                                            <span className="font-medium text-[var(--app-text)]">Method:</span>{' '}
                                            {failure.method}
                                          </p>
                                        ) : null}
                                        {failure.url ? (
                                          <p className="break-all">
                                            <span className="font-medium text-[var(--app-text)]">URL:</span>{' '}
                                            {failure.url}
                                          </p>
                                        ) : null}
                                        {failure.status ? (
                                          <p>
                                            <span className="font-medium text-[var(--app-text)]">Failure:</span>{' '}
                                            {failure.status}
                                          </p>
                                        ) : null}
                                        {failure.message ? (
                                          <p>
                                            <span className="font-medium text-[var(--app-text)]">Message:</span>{' '}
                                            {failure.message}
                                          </p>
                                        ) : null}
                                      </div>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            ) : null}
                            {specRun.requestIds.length ? (
                              <div className="mt-4 rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] p-3">
                                <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                                  Request ids captured
                                </p>
                                <div className="mt-2 max-h-44 overflow-auto pr-1">
                                  {specRun.requestIds.map((requestId) => (
                                    <div
                                      key={requestId}
                                      className="font-mono text-xs leading-6 text-[var(--app-text-secondary)]"
                                    >
                                      {requestId}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            ) : null}
                            {specRun.stepNames.length ? (
                              <div className="mt-4">
                                <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                                  Testing flow
                                </p>
                                <div className="mt-3 flex flex-wrap items-center gap-2">
                                  {specRun.stepNames.map((step, index) => (
                                    <Fragment key={step}>
                                      <span className="rounded-lg border border-[var(--app-border)] bg-[var(--app-card)] px-3 py-2 text-xs font-medium text-[var(--app-text-secondary)]">
                                        <span className="mr-2 inline-flex size-5 items-center justify-center rounded-full bg-[var(--app-accent-muted)] text-[10px] font-semibold text-[var(--app-accent)]">
                                          {index + 1}
                                        </span>
                                        {step}
                                      </span>
                                      {index < specRun.stepNames.length - 1 ? (
                                        <span className="text-[var(--app-muted)]">{'->'}</span>
                                      ) : null}
                                    </Fragment>
                                  ))}
                                </div>
                              </div>
                            ) : null}
                            {specRun.issueSnippets.length && !specRun.responseStatuses.length ? (
                              <div className="mt-4">
                                <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                                  Key issues seen in this spec
                                </p>
                                <div className="mt-2 space-y-2">
                                  {specRun.issueSnippets.map((issue) => (
                                    <div
                                      key={issue}
                                      className="rounded-md border border-rose-500/20 bg-rose-500/8 px-3 py-2 text-sm leading-relaxed text-[var(--app-text-secondary)]"
                                    >
                                      {issue}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            ) : null}
                            {specRun.failedTests.length ? (
                              <div className="mt-4">
                                <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--app-subtle)]">
                                  Failed test names
                                </p>
                                <div className="mt-2 space-y-2">
                                  {specRun.failedTests.map((testName) => (
                                    <div
                                      key={testName}
                                      className="rounded-md border border-[var(--app-border)] bg-[var(--app-card)] px-3 py-2 text-sm text-[var(--app-text-secondary)]"
                                    >
                                      {testName}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            ) : null}
                            </div>
                          </details>
                        ))}
                      </div>
                    </div>
                  ) : null}

                </div>
              </CollapsibleSection>
            ) : null}

            {activeTab === 'coverage' ? (
              <CollapsibleSection title="Coverage" defaultOpen>
                <div className="space-y-10">
                  <div>
                    <h3 className="mb-3 text-sm font-semibold tracking-tight text-[var(--app-text)]">
                      Functions on the path (source lines and hit counts)
                    </h3>
                    <PathFlowCodeCoverage run={run} />
                  </div>
                  <div className="border-t border-[var(--app-border)] pt-8">
                    <h3 className="mb-4 text-sm font-semibold tracking-tight text-[var(--app-text)]">
                      Coverage run report
                    </h3>
                    <CoverageReportHumanView run={run} />
                  </div>
                </div>
              </CollapsibleSection>
            ) : null}

            {activeTab === 'artifacts' ? (
              <>
                  <CollapsibleSection title="Generated files">
                  <ArtifactFilePanel text={run.outputTree?.join('\n')} />
                </CollapsibleSection>
                <CollapsibleSection title="Input (input.json file)">
                  <ArtifactFilePanel
                    text={
                      run.rawFiles['input.json']
                        ? `${JSON.stringify(parseJsonObject(run.rawFiles['input.json']), null, 2)}`
                        : undefined
                    }
                  />
                </CollapsibleSection>
                <CollapsibleSection title="Cypress parsed report">
                  <ArtifactFilePanel
                    text={
                      run.rawFiles['cypress_parsed.json']
                        ? `${JSON.stringify(cypressParsed ?? parseJsonObject(run.rawFiles['cypress_parsed.json']), null, 2)}`
                        : undefined
                    }
                  />
                </CollapsibleSection>
                <CollapsibleSection title="Quali bot logs">
                  <ArtifactFilePanel
                    text={
                      run.rawFiles['terminal_output.log'] ?? run.rawFiles['flow_pipeline.log']
                    }
                  />
                </CollapsibleSection>
                <CollapsibleSection title="Router logs">
                  <ArtifactFilePanel text={run.rawFiles['router_run.log']} />
                </CollapsibleSection>
                <CollapsibleSection title="Coverage HTML report">
                  {run.coverageReport?.d?.kind === 'coverage_unavailable' ? (
                    <p className="text-xs text-[var(--app-muted)]">
                      Coverage HTML was not generated for this run because no LLVM profile
                      data was produced.
                    </p>
                  ) : coverageHtmlUrl ? (
                    <iframe
                      title="Coverage HTML report"
                      src={coverageHtmlUrl}
                      className="h-[75vh] w-full rounded-xl border border-[var(--app-border)] bg-white"
                    />
                  ) : (
                    <p className="text-xs text-[var(--app-muted)]">
                      Coverage HTML is not available for this run.
                    </p>
                  )}
                </CollapsibleSection>
                <CollapsibleSection title="Coverage run report">
                  <ArtifactFilePanel
                    text={
                      run.rawFiles['coverage_run_report.json']
                        ? `${JSON.stringify(run.coverageReport ?? parseJsonObject(run.rawFiles['coverage_run_report.json']), null, 2)}`
                        : undefined
                    }
                  />
                </CollapsibleSection>
                <CollapsibleSection title="Final report">
                  <ArtifactFilePanel
                    text={
                      run.rawFiles['final_report.json']
                        ? `${JSON.stringify(run.finalReport ?? parseJsonObject(run.rawFiles['final_report.json']), null, 2)}`
                        : undefined
                    }
                  />
                </CollapsibleSection>
              </>
            ) : null}
          </div>
        ) : null}
      </main>
    </div>
  )
}
