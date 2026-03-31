import { useCallback, useEffect, useMemo } from 'react'
import {
  Background,
  BackgroundVariant,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
  type ReactFlowInstance,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Scan } from 'lucide-react'
import { buildFlowFromArtifacts, type FlowNodeData } from '@/lib/buildGraph'
import { FlowFnNode, FlowHttpNode } from '@/components/FlowNodes'
import type { FinalReport, PathFlowArtifact } from '@/types/reports'
import { useTheme } from '@/theme/useTheme'

const nodeTypes = {
  flowHttp: FlowHttpNode,
  flowFn: FlowFnNode,
}

type RFNode = Node<FlowNodeData, 'flowHttp' | 'flowFn'>

type Props = {
  pathFlow: PathFlowArtifact | undefined
  finalReport: FinalReport | undefined
  reverse: boolean
}

/** Re-fit when graph inputs change (e.g. flow direction toggle). */
function FitViewOnGraphChange({
  pathFlow,
  finalReport,
  reverse,
}: {
  pathFlow: PathFlowArtifact | undefined
  finalReport: FinalReport | undefined
  reverse: boolean
}) {
  const { fitView } = useReactFlow()
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      fitView({ padding: 0.2, duration: 220 })
    })
    return () => cancelAnimationFrame(id)
  }, [pathFlow, finalReport, reverse, fitView])
  return null
}

function ResetViewButton() {
  const { fitView } = useReactFlow()
  return (
    <Panel position="top-right" className="!m-3">
      <button
        type="button"
        onClick={() => fitView({ padding: 0.2, duration: 280 })}
        className="inline-flex items-center gap-2 rounded-xl border border-[var(--app-border)] bg-[var(--app-surface)] px-3 py-2 text-xs font-medium text-[var(--app-text-secondary)] shadow-sm transition hover:border-[color-mix(in_oklab,var(--app-accent)_40%,var(--app-border))] hover:text-[var(--app-text)]"
        title="Reset zoom and pan to fit the graph"
      >
        <Scan className="size-3.5 shrink-0" aria-hidden />
        Reset view
      </button>
    </Panel>
  )
}

function FlowCanvasInner({ pathFlow, finalReport, reverse }: Props) {
  const { theme } = useTheme()
  const dotColor = theme === 'light' ? '#d1d5db' : '#1e2836'

  const built = useMemo(
    () => buildFlowFromArtifacts(pathFlow, finalReport, { reverse }),
    [pathFlow, finalReport, reverse],
  )

  const [nodes, setNodes, onNodesChange] = useNodesState<RFNode>(built.nodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(built.edges)

  useEffect(() => {
    setNodes(built.nodes)
    setEdges(built.edges)
  }, [built, setNodes, setEdges])

  const onInit = useCallback((instance: ReactFlowInstance<RFNode, Edge>) => {
    requestAnimationFrame(() => {
      instance.fitView({ padding: 0.2 })
    })
  }, [])

  return (
    <div className="relative h-full min-h-[420px] w-full rounded-2xl border border-[var(--app-border)] bg-[var(--app-flow-bg)]">
      <ReactFlow<RFNode, Edge>
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        fitView
        onInit={onInit}
        proOptions={{ hideAttribution: true }}
        minZoom={0.15}
        maxZoom={1.75}
        defaultEdgeOptions={{
          animated: true,
        }}
      >
        <FitViewOnGraphChange
          pathFlow={pathFlow}
          finalReport={finalReport}
          reverse={reverse}
        />
        <ResetViewButton />
        <Background
          variant={BackgroundVariant.Dots}
          gap={20}
          size={1}
          color={dotColor}
        />
      </ReactFlow>
    </div>
  )
}

export function FlowCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <FlowCanvasInner {...props} />
    </ReactFlowProvider>
  )
}
