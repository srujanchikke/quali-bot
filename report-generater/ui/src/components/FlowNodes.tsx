import { memo } from 'react'
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import { motion } from 'framer-motion'
import {
  AlertCircle,
  CheckCircle2,
  CircleSlash2,
  Globe,
  Braces,
} from 'lucide-react'
import { displayChainRoleLabel } from '@/lib/chainRoleLabel'
import type { FlowNodeData } from '@/lib/buildGraph'

function StatusIcon({
  status,
  unreached,
}: {
  status?: FlowNodeData['status']
  unreached?: boolean
}) {
  if (unreached)
    return (
      <CircleSlash2
        className="size-3.5 shrink-0 text-[var(--app-muted)]"
        aria-hidden
      />
    )
  if (status === 'error')
    return <AlertCircle className="size-3.5 shrink-0 text-red-500" aria-hidden />
  if (status === 'warn')
    return <AlertCircle className="size-3.5 shrink-0 text-amber-500" aria-hidden />
  return (
    <CheckCircle2 className="size-3.5 shrink-0 text-emerald-500" aria-hidden />
  )
}

type HttpNode = Node<FlowNodeData, 'flowHttp'>

export const FlowHttpNode = memo(function FlowHttpNode(
  props: NodeProps<HttpNode>,
) {
  const { data } = props
  return (
    <motion.div
      layout
      initial={{ opacity: 0, scale: 0.96 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ type: 'spring', stiffness: 380, damping: 28 }}
      style={{ boxShadow: 'var(--app-node-shadow)' }}
      className={`w-[min(100vw-2rem,248px)] rounded-xl border bg-[var(--app-card)] p-3 ${
        data.status === 'error'
          ? 'border-red-500/50 ring-1 ring-red-500/30'
          : 'border-[var(--app-border)] ring-1 ring-[color-mix(in_oklab,var(--app-accent)_22%,transparent)]'
      }`}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!size-2 !border !border-[var(--app-border)] !bg-[var(--app-handle-bg)]"
      />
      <Handle
        type="source"
        position={Position.Right}
        className="!size-2 !border !border-[var(--app-border)] !bg-[var(--app-accent)]"
      />
      <div className="flex items-start gap-2.5">
        <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-[var(--app-accent-muted)] text-[var(--app-accent)]">
          <Globe className="size-4" aria-hidden />
        </div>
        <div className="min-w-0 flex-1 text-left">
          <p className="text-[9px] font-medium uppercase tracking-wider text-[var(--app-muted)]">
            {data.roleLabel ?? 'API'}
          </p>
          <p
            className="break-words text-[12px] font-semibold leading-snug text-[var(--app-text)] [overflow-wrap:anywhere]"
            title={data.title}
          >
            {data.title}
          </p>
          {data.subtitle ? (
            <p className="mt-0.5 text-[10px] leading-snug text-[var(--app-subtle)]">
              {data.subtitle}
            </p>
          ) : null}
          {data.httpStatus != null ? (
            <p className="mt-1.5 inline-flex items-center gap-1 rounded-full bg-[var(--app-http-badge-bg)] px-1.5 py-0.5 text-[9px] font-mono text-[var(--app-muted)]">
              HTTP {data.httpStatus}
            </p>
          ) : null}
        </div>
        <StatusIcon status={data.status} />
      </div>
      {data.error?.message ? (
        <p
          className="mt-2 rounded-md border px-2 py-1.5 text-left text-[10px] leading-snug [overflow-wrap:anywhere]"
          style={{
            borderColor: 'var(--app-error-border)',
            background: 'rgba(239, 68, 68, 0.08)',
            color: 'var(--app-error-fg)',
          }}
          title={
            data.error.message
              ? `${data.error.code ?? ''}: ${data.error.message}`.trim()
              : undefined
          }
        >
          <span
            className="font-mono text-[8px]"
            style={{ color: 'var(--app-error-code)' }}
          >
            {data.error.code ?? 'error'}
          </span>
          <br />
          {data.error.message}
        </p>
      ) : null}
    </motion.div>
  )
})

type FnNode = Node<FlowNodeData, 'flowFn'>

export const FlowFnNode = memo(function FlowFnNode(props: NodeProps<FnNode>) {
  const { data } = props
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: 'spring', stiffness: 420, damping: 32 }}
      style={{ boxShadow: 'var(--app-node-shadow)' }}
      className={`w-[min(100vw-2rem,248px)] rounded-xl border bg-[var(--app-card)] p-3 ${
        data.unreached
          ? 'border-[var(--app-border)] opacity-75 ring-0'
          : data.status === 'error'
            ? 'border-red-500/45 ring-1 ring-red-500/25'
            : data.role === 'target'
              ? 'border-[color-mix(in_oklab,var(--app-accent)_35%,var(--app-border))] ring-1 ring-[color-mix(in_oklab,var(--app-accent)_15%,transparent)]'
              : 'border-[var(--app-border)]'
      }`}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!size-2 !border !border-[var(--app-border)] !bg-[color-mix(in_oklab,var(--app-accent)_80%,var(--app-border))]"
      />
      <Handle
        type="source"
        position={Position.Right}
        className="!size-2 !border !border-[var(--app-border)] !bg-[var(--app-accent)]"
      />
      <div className="flex items-start gap-2.5">
        <div
          className={`flex size-8 shrink-0 items-center justify-center rounded-lg ${
            data.role === 'target'
              ? 'bg-[var(--app-accent-muted)] text-[var(--app-accent)]'
              : 'bg-[var(--app-fn-icon-bg)] text-[var(--app-muted)]'
          }`}
        >
          <Braces className="size-4" aria-hidden />
        </div>
        <div className="min-w-0 flex-1 text-left">
          <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0">
            <p className="text-[9px] font-medium uppercase tracking-wider text-[var(--app-muted)]">
              {data.roleLabel ?? displayChainRoleLabel(data.role)}
            </p>
            {data.stepTotal != null && data.stepTotal > 0 ? (
              <span className="text-[8px] font-mono text-[var(--app-subtle)]">
                {data.stepIndex ?? '—'} / {data.stepTotal}
              </span>
            ) : null}
          </div>
          <p
            className="break-words font-mono text-[11px] font-semibold leading-snug text-[var(--app-text)] [overflow-wrap:anywhere]"
            title={data.title}
          >
            {data.title}
          </p>
          {data.file ? (
            <p
              className="mt-1 break-words text-[9px] leading-snug text-[var(--app-subtle)] [overflow-wrap:anywhere]"
              title={`${data.file}${data.line != null && data.line > 0 ? `:${data.line}` : ''}`}
            >
              {data.file}
              {data.line != null && data.line > 0 ? `:${data.line}` : ''}
            </p>
          ) : null}
          {data.unreached ? (
            <p className="mt-1.5 text-[9px] leading-snug text-[var(--app-muted)]">
              Not reached — failure earlier in the chain.
            </p>
          ) : null}
        </div>
        <StatusIcon status={data.status} unreached={data.unreached} />
      </div>
      {data.error?.message ? (
        <div
          className="mt-2 space-y-0.5 rounded-md border px-2 py-1.5 text-left text-[10px] leading-snug [overflow-wrap:anywhere]"
          style={{
            borderColor: 'var(--app-error-border)',
            background: 'rgba(239, 68, 68, 0.08)',
            color: 'var(--app-error-fg)',
          }}
          title={
            data.error.message
              ? `${data.error.code ?? ''}: ${data.error.message}`.trim()
              : undefined
          }
        >
          <p
            className="font-mono text-[8px]"
            style={{ color: 'var(--app-error-code)' }}
          >
            {data.error.code ?? 'error'}
          </p>
          <p>{data.error.message}</p>
        </div>
      ) : null}
    </motion.div>
  )
})
