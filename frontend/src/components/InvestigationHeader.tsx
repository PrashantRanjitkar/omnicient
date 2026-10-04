import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import type { InvestigationDetail } from '../types'
import { api } from '../api/client'
import { formatDate } from '../lib/display'

export type WorkspaceView = 'list' | 'graph'

interface Props {
  investigation: InvestigationDetail
  onRecrawl: () => void
  onResetLayout: () => void
  busy: boolean
  view: WorkspaceView
}

const STATUS_STYLE: Record<string, string> = {
  CREATED: 'text-faint border-line',
  CRAWLING: 'text-accent border-accent/60',
  ANALYZING: 'text-accent border-accent/60',
  COMPLETED: 'text-confirmed border-confirmed/50',
  FAILED: 'text-rejected border-rejected/60',
}

/** Top bar of the investigation workspace. */
export default function InvestigationHeader({
  investigation,
  onRecrawl,
  onResetLayout,
  busy,
  view,
}: Props) {
  const running = ['CREATED', 'CRAWLING', 'ANALYZING'].includes(investigation.status)

  return (
    <header className="flex items-center justify-between gap-4 border-b border-line bg-panel px-4 py-2">
      <div className="flex min-w-0 items-center gap-4">
        <Link to="/" className="font-mono text-[15px] font-semibold tracking-[0.2em] text-accent">
          OMNICIENT
        </Link>
        <div className="min-w-0">
          <div className="truncate text-[13px] text-ink">{investigation.name}</div>
          <div className="truncate font-mono text-[11px] text-faint">
            seed: {investigation.seed_platform}/@{investigation.seed_identifier} ·
            depth {investigation.max_depth} · created {formatDate(investigation.created_at)}
          </div>
        </div>
        {investigation.demo && (
          <span className="rounded border border-demo/50 bg-demo/10 px-2 py-0.5 font-mono text-[10px] tracking-wider text-demo">
            DEMO DATA
          </span>
        )}
        <span
          className={`rounded border px-2 py-0.5 font-mono text-[10px] tracking-wider ${
            STATUS_STYLE[investigation.status] ?? 'text-faint border-line'
          }`}
        >
          {running && <span className="mr-1 animate-pulse">●</span>}
          {investigation.status}
        </span>
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {/*
          Two readings of the same investigation, on two routes. The results
          answer "what came back from each source", which is the first
          question; the graph answers "how do these connect", which is the
          second. Separate URLs so each is linkable on its own.
        */}
        <nav className="flex overflow-hidden rounded border border-line">
          {(
            [
              ['list', 'Results', `/investigations/${investigation.id}`],
              ['graph', 'Graph', `/investigations/${investigation.id}/graph`],
            ] as const
          ).map(([id, label, to]) => (
            <Link
              key={id}
              to={to}
              className="px-2.5 py-1 font-mono text-[11px] tracking-wide"
              style={{
                color: view === id ? 'var(--color-void)' : 'var(--color-dim)',
                background: view === id ? 'var(--color-accent)' : 'transparent',
              }}
            >
              {label}
            </Link>
          ))}
        </nav>

        {view === 'graph' && (
          <button
            onClick={onResetLayout}
            className="rounded border border-line px-2 py-1 text-[12px] text-dim hover:border-line-bright hover:text-ink"
          >
            Reset layout
          </button>
        )}
        <button
          onClick={onRecrawl}
          disabled={busy || running}
          className="rounded border border-line px-2 py-1 text-[12px] text-dim hover:border-line-bright hover:text-ink disabled:opacity-40"
        >
          {busy ? 'Crawling…' : 'Re-run discovery'}
        </button>
        <ExportMenu investigationId={investigation.id} />
        <Link
          to="/"
          className="rounded border border-accent/60 bg-accent/10 px-2 py-1 text-[12px] font-medium text-accent hover:bg-accent/20"
        >
          New investigation
        </Link>
      </div>
    </header>
  )
}

const EXPORTS = [
  {
    format: 'pdf',
    label: 'PDF report',
    note: 'Readable case report: findings, evidence, decisions, method',
  },
  {
    format: 'json',
    label: 'JSON',
    note: 'Full record: entities, relationships, evidence, snapshots, timeline',
  },
  { format: 'csv', label: 'CSV', note: 'One row per relationship, with its evidence' },
] as const

/**
 * One menu for every way out of the app. The report is first because it is
 * the one a person reads; the data exports are for tools.
 */
function ExportMenu({ investigationId }: { investigationId: string }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === 'Escape' :
          !root.current?.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', close)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', close)
    }
  }, [open])

  return (
    <div ref={root} className="relative">
      <button
        onClick={() => setOpen((shown) => !shown)}
        aria-expanded={open}
        className="rounded border border-line px-2 py-1 text-[12px] text-dim hover:border-line-bright hover:text-ink"
      >
        Export ▾
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-1 w-72 rounded border border-line bg-panel p-1 shadow-lg">
          {EXPORTS.map(({ format, label, note }) => (
            <a
              key={format}
              href={api.exportUrl(investigationId, format)}
              onClick={() => setOpen(false)}
              className="block rounded px-2 py-1.5 hover:bg-raised"
            >
              <div className="text-[12px] text-ink">{label}</div>
              <div className="text-[11px] leading-snug text-faint">{note}</div>
            </a>
          ))}
        </div>
      )}
    </div>
  )
}
