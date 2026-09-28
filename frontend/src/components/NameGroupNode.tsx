import { Handle, Position, type NodeProps } from '@xyflow/react'
import type { GraphGroup } from '../types'

export interface NameGroupNodeData extends Record<string, unknown> {
  group: GraphGroup
}

/**
 * One card standing in for every account that shares the searched handle and
 * nothing else.
 *
 * A bare-username crawl asks every source for the name, and on a common
 * handle most of what comes back is somebody else. One card each, they filled
 * the canvas and - hanging from the same handle - read as if they belonged
 * together. Folded, the card says what they are: accounts that use this name,
 * with no other evidence yet. Clicking it lays them out individually.
 */
export default function NameGroupNode({ data }: NodeProps) {
  const { group } = data as NameGroupNodeData
  const shown = group.platforms.slice(0, 4)
  const more = group.platforms.length - shown.length

  return (
    <div
      className="cursor-pointer rounded-md border border-dashed bg-panel/90 px-3 py-2 transition-colors hover:bg-raised"
      style={{ minWidth: 168, maxWidth: 208, borderColor: 'var(--color-line-bright)' }}
      title="Accounts found only because they use the same handle. Click to show them one by one."
    >
      <Handle
        type="target"
        position={Position.Top}
        style={{
          width: 7,
          height: 7,
          background: 'var(--color-panel)',
          border: '1px solid var(--color-line-bright)',
        }}
      />
      <div className="panel-title">Same name only</div>
      <div className="mt-1 text-[13px] text-ink">
        {group.member_ids.length} accounts named{' '}
        <span className="font-mono">@{group.handle}</span>
      </div>
      <div className="mt-0.5 truncate text-[11px] text-faint">
        {shown.join(' · ')}
        {more > 0 && ` +${more}`}
      </div>
      <div className="mt-1.5 font-mono text-[10px] text-dim">
        no other evidence yet · click to show
      </div>
    </div>
  )
}
