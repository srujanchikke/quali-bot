import type { Edge, Node } from '@xyflow/react'
import { displayChainRoleLabel } from '@/lib/chainRoleLabel'
import type {
  ApiError,
  FinalReport,
  PathFlowArtifact,
  PathFlowChainStep,
} from '@/types/reports'

export type FlowNodeData = Record<string, unknown> & {
  kind: 'http' | 'function'
  title: string
  subtitle?: string
  file?: string
  line?: number
  /** Raw role from artifact (`target`, `context`, …) — kept for styling. */
  role?: string
  /** Human label: API, Chain, Leaf, … */
  roleLabel?: string
  source?: string
  status?: 'ok' | 'error' | 'warn'
  error?: { code?: string; message?: string }
  httpStatus?: number
  /** Execution did not reach this step (failure earlier). */
  unreached?: boolean
  stepIndex?: number
  stepTotal?: number
}

/** Horizontal distance between node *left* edges. */
const X_STEP = 360
const Y = 100

type FailurePlacement =
  | { kind: 'none' }
  | { kind: 'api'; error: ApiError }
  | { kind: 'chain'; forwardIndex: number; error: ApiError }

/**
 * Decide whether the surfaced API error belongs at the HTTP boundary or on a
 * specific chain step (usually the leaf). Uses `root_cause_analysis` when present.
 */
function resolveFailurePlacement(
  final: FinalReport | undefined,
  chainLen: number,
): FailurePlacement {
  const err = final?.api_call?.error
  if (!err || (!err.message && !err.code)) return { kind: 'none' }

  if (chainLen === 0) return { kind: 'api', error: err }

  const rca = final?.root_cause_analysis
  const blob = `${rca?.reason ?? ''} ${rca?.why_coverage_is_zero ?? ''} ${rca?.status ?? ''}`

  const beforeLeaf = /rejected before leaf|before leaf execution|did not enter leaf|did not enter|pre-check|auth failure|not enter leaf/i.test(
    blob,
  )

  if (beforeLeaf) return { kind: 'api', error: err }

  // Leaf / terminal function: coverage or handler-side failure
  return { kind: 'chain', forwardIndex: chainLen - 1, error: err }
}

/**
 * Builds nodes left → right: API → chain steps → leaf (`target`).
 * `reverse`: show chain from leaf toward HTTP (positions still API-first).
 */
export function buildFlowFromArtifacts(
  pathFlow: PathFlowArtifact | undefined,
  finalReport: FinalReport | undefined,
  options?: { reverse?: boolean },
): { nodes: Node<FlowNodeData, 'flowHttp' | 'flowFn'>[]; edges: Edge[] } {
  const flow = pathFlow?.flows?.[0]
  const ep = pathFlow?.endpoints?.[0] ?? flow?.endpoints?.[0]
  const chainForward: PathFlowChainStep[] = flow?.chain ?? []

  const displayChain = options?.reverse
    ? [...chainForward].reverse()
    : chainForward

  const nodes: Node<FlowNodeData, 'flowHttp' | 'flowFn'>[] = []
  const edges: Edge[] = []

  const httpTitle = ep
    ? `${ep.method} ${ep.path}`
    : finalReport?.api_call?.endpoint
      ? `${finalReport.api_call.method ?? '—'} ${finalReport.api_call.endpoint}`
      : 'HTTP route'

  const httpStatus = finalReport?.api_call?.http_status_code
  const apiErr = finalReport?.api_call?.error
  const httpStatusBad = httpStatus != null && httpStatus >= 400

  const placement = resolveFailurePlacement(finalReport, chainForward.length)

  const errorOnHttp = placement.kind === 'api'
  const httpNodeStatus: FlowNodeData['status'] = errorOnHttp
    ? 'error'
    : httpStatusBad
      ? 'warn'
      : 'ok'

  nodes.push({
    id: 'n-http',
    type: 'flowHttp',
    position: { x: 0, y: Y },
    data: {
      kind: 'http',
      title: httpTitle,
      subtitle: finalReport?.api_call?.api_flow ?? 'Handler chain entry',
      roleLabel: 'API',
      status: httpNodeStatus,
      httpStatus,
      error: errorOnHttp ? apiErr : undefined,
    },
  })

  displayChain.forEach((step, displayIdx) => {
    const forwardIndex =
      options?.reverse === true
        ? chainForward.length - 1 - displayIdx
        : displayIdx
    const id = `n-fn-${forwardIndex}`

    const errHere =
      placement.kind === 'chain' && placement.forwardIndex === forwardIndex
        ? placement.error
        : undefined

    const unreached =
      placement.kind === 'api' ||
      (placement.kind === 'chain' && forwardIndex > placement.forwardIndex)

    const nodeStatus: FlowNodeData['status'] = unreached
      ? 'warn'
      : errHere
        ? 'error'
        : 'ok'

    const label = displayChainRoleLabel(step.role)

    nodes.push({
      id,
      type: 'flowFn',
      position: { x: X_STEP * (displayIdx + 1), y: Y },
      data: {
        kind: 'function',
        title: step.function,
        subtitle: unreached ? undefined : displayChainRoleLabel(step.role),
        file: step.file,
        line: step.def_line,
        role: step.role,
        roleLabel: label,
        source: step.source,
        status: nodeStatus,
        error: errHere,
        unreached,
        stepIndex: forwardIndex + 1,
        stepTotal: chainForward.length,
      },
    })

    const prevId =
      displayIdx === 0
        ? 'n-http'
        : `n-fn-${
            options?.reverse === true
              ? forwardIndex + 1
              : forwardIndex - 1
          }`

    edges.push({
      id: `e-${prevId}-${id}`,
      source: prevId,
      target: id,
      // Keep animated: true so React Flow's dash animation runs; unreached is shown via stroke only.
      animated: true,
      style: {
        stroke: unreached ? 'var(--app-edge-muted)' : 'var(--app-accent)',
        strokeWidth: unreached ? 1.5 : 2,
      },
      type: 'smoothstep',
    })
  })

  if (chainForward.length === 0 && finalReport?.coverage_diff?.leaf?.name) {
    const leaf = finalReport.coverage_diff.leaf
    const leafUnreached = placement.kind === 'api'
    nodes.push({
      id: 'n-leaf-fallback',
      type: 'flowFn',
      position: { x: X_STEP, y: Y },
      data: {
        kind: 'function',
        title: leaf.name ?? 'leaf',
        subtitle: 'From coverage report',
        file: leaf.file,
        line: leaf.def_line,
        role: 'target',
        roleLabel: 'Leaf',
        status: leafUnreached ? 'warn' : 'ok',
        error: undefined,
        unreached: leafUnreached,
        stepIndex: 1,
        stepTotal: 1,
      },
    })
    edges.push({
      id: 'e-http-fallback',
      source: 'n-http',
      target: 'n-leaf-fallback',
      animated: true,
      style: {
        stroke: leafUnreached ? 'var(--app-edge-muted)' : 'var(--app-accent)',
        strokeWidth: leafUnreached ? 1.5 : 2,
      },
      type: 'smoothstep',
    })
  }

  return { nodes, edges }
}
