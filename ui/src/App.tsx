import { useAppApi } from '@kirocrew/app-sdk'
import { Card, PageHeader, Btn } from '@kirocrew/app-sdk/ui'
import { useState, useEffect, useCallback } from 'react'

// ---------------------------------------------------------------------------
// Tipos
// ---------------------------------------------------------------------------

interface Issue {
  number: number | string
  title: string
  repo: string
  url: string
  age_min: number
  labels: string[]
  blocked: boolean
  running: boolean
}

interface Columns {
  todo: Issue[]
  dev: Issue[]
  review: Issue[]
  reviewed: Issue[]
  done: Issue[]
  blocked: Issue[]
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatAge(minutes: number): string {
  if (minutes < 60) return `${minutes}m`
  if (minutes < 60 * 24) return `${Math.floor(minutes / 60)}h`
  return `${Math.floor(minutes / (60 * 24))}d`
}

function repoShort(repo: string): string {
  return repo.split('/').pop() ?? repo
}

// ---------------------------------------------------------------------------
// IssueCard
// ---------------------------------------------------------------------------

interface IssueCardProps {
  issue: Issue
  showDispatch?: boolean
  onDispatch?: (repo: string, number: number | string) => Promise<void>
  dispatching?: boolean
}

function IssueCard({ issue, showDispatch, onDispatch, dispatching }: IssueCardProps) {
  return (
    <Card style={{ marginBottom: 8, padding: '10px 12px' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <span style={{ fontWeight: 600, fontSize: 12, opacity: 0.6, whiteSpace: 'nowrap' }}>
              #{issue.number}
            </span>
            <span style={{ fontWeight: 500, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {issue.title}
            </span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11, opacity: 0.6 }}>
            <span>{repoShort(issue.repo)}</span>
            {issue.age_min > 0 && (
              <span>⏱ {formatAge(issue.age_min)}</span>
            )}
            {issue.running && (
              <span style={{ color: '#f97316' }}>● running</span>
            )}
          </div>
        </div>
        {showDispatch && onDispatch && (
          <Btn
            size="sm"
            variant="secondary"
            disabled={dispatching}
            onClick={() => onDispatch(issue.repo, issue.number)}
            style={{ flexShrink: 0, fontSize: 11 }}
          >
            {dispatching ? '...' : 'Dispatch'}
          </Btn>
        )}
      </div>
      {issue.url && (
        <a
          href={issue.url}
          target="_blank"
          rel="noreferrer"
          style={{ fontSize: 11, opacity: 0.5, textDecoration: 'none', display: 'block', marginTop: 4 }}
        >
          🔗 Ver issue
        </a>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Column
// ---------------------------------------------------------------------------

interface ColumnProps {
  title: string
  issues: Issue[]
  color: string
  showDispatch?: boolean
  onDispatch?: (repo: string, number: number | string) => Promise<void>
  dispatchingKey?: string
}

function Column({ title, issues, color, showDispatch, onDispatch, dispatchingKey }: ColumnProps) {
  return (
    <div style={{ flex: 1, minWidth: 180, maxWidth: 260 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 10,
          paddingBottom: 6,
          borderBottom: `2px solid ${color}`,
        }}
      >
        <span style={{ fontWeight: 700, fontSize: 13, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
          {title}
        </span>
        <span
          style={{
            background: color,
            color: '#fff',
            borderRadius: 10,
            padding: '1px 7px',
            fontSize: 11,
            fontWeight: 700,
          }}
        >
          {issues.length}
        </span>
      </div>
      {issues.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.4, textAlign: 'center', padding: '16px 0' }}>—</div>
      ) : (
        issues.map((issue) => {
          const key = `${issue.repo}#${issue.number}`
          return (
            <IssueCard
              key={key}
              issue={issue}
              showDispatch={showDispatch}
              onDispatch={onDispatch}
              dispatching={dispatchingKey === key}
            />
          )
        })
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Mock data — usado quando o backend.routes ainda não está disponível
// (issue #170: backend.routes não registra rotas para apps de terceiros)
// ---------------------------------------------------------------------------

const MOCK_COLUMNS: Columns = {
  todo: [
    { number: 99, title: 'Exemplo: feature aguardando dev', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 45, labels: ['crewflow:feature', 'crewflow:p2'], blocked: false, running: false },
  ],
  dev: [
    { number: 100, title: 'Exemplo: issue em implementação', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 120, labels: ['crewflow:bug', 'crewflow:p1'], blocked: false, running: true },
  ],
  review: [
    { number: 101, title: 'Exemplo: PR aguardando review', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 30, labels: ['crewflow:feature'], blocked: false, running: false },
  ],
  reviewed: [],
  done: [],
  blocked: [
    { number: 102, title: 'Exemplo: issue bloqueada', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 240, labels: ['crewflow:debt'], blocked: true, running: false },
  ],
}


export default function CrewFlow() {
  const api = useAppApi()
  const [columns, setColumns] = useState<Columns>({
    todo: [],
    dev: [],
    review: [],
    reviewed: [],
    done: [],
    blocked: [],
  })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [isMock, setIsMock] = useState(false)
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)
  const [dispatchingKey, setDispatchingKey] = useState<string | null>(null)

  const load = useCallback(() => {
    return api
      .get('/api/apps/kirocrew-flow/issues')
      .then((d: { columns?: Columns }) => {
        setColumns(d.columns ?? { todo: [], dev: [], review: [], reviewed: [], done: [], blocked: [] })
        setLastUpdate(new Date())
        setError(null)
        setIsMock(false)
      })
      .catch((err: unknown) => {
        const msg = String(err)
        // 404 = backend.routes não disponível para apps de terceiros (issue #170)
        // Usar dados mock para permitir desenvolvimento da UI
        if (msg.includes('404') || msg.includes('not found')) {
          setColumns(MOCK_COLUMNS)
          setIsMock(true)
          setError(null)
        } else {
          setError(msg)
        }
      })
      .finally(() => {
        setLoading(false)
      })
  }, [api])

  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [load])

  const handleDispatch = useCallback(
    async (repo: string, number: number | string) => {
      const key = `${repo}#${number}`
      setDispatchingKey(key)
      try {
        await api.post('/api/apps/kirocrew-flow/dispatch', { repo, number: Number(number) })
        await load()
      } catch (err) {
        console.error('dispatch failed:', err)
      } finally {
        setDispatchingKey(null)
      }
    },
    [api, load],
  )

  const stageColumns: Array<{ key: keyof Columns; title: string; color: string; showDispatch?: boolean }> = [
    { key: 'todo', title: 'Todo', color: '#16a34a', showDispatch: true },
    { key: 'dev', title: 'Dev', color: '#2563eb' },
    { key: 'review', title: 'Review', color: '#8b5cf6' },
    { key: 'reviewed', title: 'QA', color: '#0ea5e9' },
    { key: 'done', title: 'Done', color: '#22c55e' },
  ]

  const totalActive = (
    columns.todo.length +
    columns.dev.length +
    columns.review.length +
    columns.reviewed.length +
    columns.blocked.length
  )

  return (
    <div style={{ padding: '20px 24px', maxWidth: 1400 }}>
      <PageHeader
        title="KiroCrew Flow"
        subtitle={
          loading
            ? 'Carregando…'
            : error
            ? `Erro: ${error}`
            : isMock
            ? '⚠️ Modo demo — backend indisponível (issue #170)'
            : lastUpdate
            ? `${totalActive} issues ativas · atualizado ${lastUpdate.toLocaleTimeString()}`
            : ''
        }
        actions={
          <Btn size="sm" variant="secondary" onClick={load} disabled={loading}>
            ↻ Atualizar
          </Btn>
        }
      />

      {/* Kanban principal — 5 colunas de estágio */}
      <div
        style={{
          display: 'flex',
          gap: 16,
          overflowX: 'auto',
          marginTop: 20,
          paddingBottom: 8,
        }}
      >
        {stageColumns.map(({ key, title, color, showDispatch }) => (
          <Column
            key={key}
            title={title}
            issues={columns[key]}
            color={color}
            showDispatch={showDispatch}
            onDispatch={showDispatch ? handleDispatch : undefined}
            dispatchingKey={dispatchingKey ?? undefined}
          />
        ))}
      </div>

      {/* Coluna blocked separada abaixo */}
      {columns.blocked.length > 0 && (
        <div style={{ marginTop: 24 }}>
          <Column
            title="Blocked"
            issues={columns.blocked}
            color="#dc2626"
          />
        </div>
      )}
    </div>
  )
}
