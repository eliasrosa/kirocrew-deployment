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
  implicit_state?: string | null
}

interface Columns {
  spec: Issue[]
  ready: Issue[]
  todo: Issue[]
  dev: Issue[]
  review: Issue[]
  review_ok: Issue[]
  reviewed: Issue[]
  done: Issue[]
  blocked: Issue[]
}

interface ApiResponse {
  squad_name?: string
  project?: string
  columns?: Columns
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

function emptyColumns(): Columns {
  return {
    spec: [],
    ready: [],
    todo: [],
    dev: [],
    review: [],
    review_ok: [],
    reviewed: [],
    done: [],
    blocked: [],
  }
}

// ---------------------------------------------------------------------------
// Mock data — usado quando o backend.routes ainda não está disponível
// ---------------------------------------------------------------------------

const MOCK_RESPONSE: ApiResponse = {
  squad_name: 'KiroCrew Flow (demo)',
  project: 'eliasrosa/kirocrew-flow',
  columns: {
    spec: [
      { number: 95, title: 'Exemplo: feature sendo especificada', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 240, labels: ['crewflow:spec', 'crewflow:feature'], blocked: false, running: false },
    ],
    ready: [
      { number: 97, title: 'Exemplo: spec pronta, aguardando priorização', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 120, labels: ['crewflow:ready', 'crewflow:feature'], blocked: false, running: false },
    ],
    todo: [
      { number: 99, title: 'Exemplo: feature aguardando dev', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 45, labels: ['crewflow:feature', 'crewflow:p2'], blocked: false, running: false },
    ],
    dev: [
      { number: 100, title: 'Exemplo: issue em implementação', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 120, labels: ['crewflow:bug', 'crewflow:p1'], blocked: false, running: true },
      { number: 104, title: 'Exemplo: divergência — label=dev mas sem branch', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 30, labels: ['crewflow:feature'], blocked: false, running: false, implicit_state: 'todo' },
    ],
    review: [
      { number: 101, title: 'Exemplo: PR aguardando review', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 30, labels: ['crewflow:feature'], blocked: false, running: false, implicit_state: 'review' },
    ],
    review_ok: [
      { number: 103, title: 'Exemplo: PR aprovado, aguardando merge', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 15, labels: ['crewflow:feature', 'crewflow:review-ok'], blocked: false, running: false },
    ],
    reviewed: [],
    done: [],
    blocked: [
      { number: 102, title: 'Exemplo: issue bloqueada', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 240, labels: ['crewflow:debt'], blocked: true, running: false },
    ],
  },
}

// ---------------------------------------------------------------------------
// Shadow mode — mapeamento coluna → implicit_state equivalente
// ---------------------------------------------------------------------------

// Mapa de coluna kanban → nome de implicit_state equivalente
// (para detectar divergência entre onde a issue está e o que as evidências indicam)
const COLUMN_TO_IMPLICIT: Record<string, string> = {
  todo: 'todo',
  dev: 'dev',
  review: 'review',
  reviewed: 'review',  // QA está na coluna reviewed; implicit equivalente é review/review_ok
  done: 'done',
  blocked: '',
}

function hasDivergence(issue: Issue, column: string): boolean {
  if (!issue.implicit_state) return false
  const expected = COLUMN_TO_IMPLICIT[column] ?? ''
  if (!expected) return false
  // review_ok está na coluna review — não é divergência
  if (column === 'review' && issue.implicit_state === 'review_ok') return false
  return issue.implicit_state !== expected
}

// ---------------------------------------------------------------------------
// IssueCard
// ---------------------------------------------------------------------------

interface IssueCardProps {
  issue: Issue
  showDispatch?: boolean
  onDispatch?: (repo: string, number: number | string) => Promise<void>
  dispatching?: boolean
  column?: string
}

function IssueCard({ issue, showDispatch, onDispatch, dispatching, column = '' }: IssueCardProps) {
  const divergent = hasDivergence(issue, column)
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
            {divergent && issue.implicit_state && (
              <span
                title={`Estado implícito: ${issue.implicit_state} · Label atual: ${column}`}
                style={{
                  color: '#dc2626',
                  fontWeight: 700,
                  background: '#fee2e2',
                  borderRadius: 4,
                  padding: '1px 5px',
                  fontSize: 10,
                  letterSpacing: '0.02em',
                }}
              >
                ⚠ impl: {issue.implicit_state}
              </span>
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
// SubColumn — coluna dentro de um painel (ex: Aguardando / Trabalhando)
// ---------------------------------------------------------------------------

interface SubColumnProps {
  title: string
  issues: Issue[]
  color: string
  showDispatch?: boolean
  onDispatch?: (repo: string, number: number | string) => Promise<void>
  dispatchingKey?: string
  columnKey?: string
}

function SubColumn({ title, issues, color, showDispatch, onDispatch, dispatchingKey, columnKey = '' }: SubColumnProps) {
  return (
    <div style={{ flex: 1, minWidth: 160 }}>
      <div style={{
        fontSize: 11,
        fontWeight: 600,
        textTransform: 'uppercase',
        letterSpacing: '0.06em',
        opacity: 0.55,
        marginBottom: 8,
        paddingBottom: 4,
        borderBottom: `1px solid ${color}44`,
        display: 'flex',
        alignItems: 'center',
        gap: 6,
      }}>
        {title}
        <span style={{
          background: color + '33',
          color: color,
          borderRadius: 8,
          padding: '0 6px',
          fontSize: 10,
          fontWeight: 700,
        }}>
          {issues.length}
        </span>
      </div>
      {issues.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.3, textAlign: 'center', padding: '12px 0' }}>—</div>
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
              column={columnKey}
            />
          )
        })
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// WaitingSection — seção de espera humana (SPEC ou READY)
// ---------------------------------------------------------------------------

interface WaitingSectionProps {
  title: string
  subtitle: string
  issues: Issue[]
  accentColor: string
}

function WaitingSection({ title, subtitle, issues, accentColor }: WaitingSectionProps) {
  return (
    <div style={{
      flex: 1,
      border: `1px solid ${accentColor}33`,
      borderRadius: 10,
      padding: '14px 16px',
      background: `${accentColor}08`,
      minWidth: 220,
    }}>
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontWeight: 700, fontSize: 13, color: accentColor, marginBottom: 2 }}>
          {title}
        </div>
        <div style={{ fontSize: 11, opacity: 0.5 }}>{subtitle}</div>
      </div>
      {issues.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.3, textAlign: 'center', padding: '12px 0' }}>—</div>
      ) : (
        issues.map((issue) => (
          <IssueCard key={`${issue.repo}#${issue.number}`} issue={issue} />
        ))
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// AgentsPanel — painel duplo Desenvolvimento + Code Review
// ---------------------------------------------------------------------------

interface AgentsPanelProps {
  columns: Columns
  onDispatch: (repo: string, number: number | string) => Promise<void>
  dispatchingKey: string | null
}

function AgentsPanel({ columns, onDispatch, dispatchingKey }: AgentsPanelProps) {
  return (
    <div style={{
      border: '1px solid rgba(128,128,128,0.15)',
      borderRadius: 10,
      padding: '14px 16px',
      marginTop: 16,
    }}>
      <div style={{ fontWeight: 700, fontSize: 12, opacity: 0.4, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 14 }}>
        Agentes
      </div>

      <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap' }}>
        {/* Painel: Desenvolvimento */}
        <div style={{ flex: 1, minWidth: 300 }}>
          <div style={{
            fontWeight: 700,
            fontSize: 13,
            marginBottom: 12,
            paddingBottom: 6,
            borderBottom: '2px solid #2563eb',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}>
            <span>Desenvolvimento</span>
            <span style={{
              background: '#2563eb',
              color: '#fff',
              borderRadius: 10,
              padding: '1px 7px',
              fontSize: 11,
              fontWeight: 700,
            }}>
              {columns.todo.length + columns.dev.length}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 12 }}>
            <SubColumn
              title="Aguardando"
              issues={columns.todo}
              color="#16a34a"
              showDispatch
              onDispatch={onDispatch}
              dispatchingKey={dispatchingKey ?? undefined}
              columnKey="todo"
            />
            <SubColumn
              title="Trabalhando"
              issues={columns.dev}
              color="#2563eb"
              columnKey="dev"
            />
          </div>
        </div>

        {/* Painel: Code Review */}
        <div style={{ flex: 1, minWidth: 300 }}>
          <div style={{
            fontWeight: 700,
            fontSize: 13,
            marginBottom: 12,
            paddingBottom: 6,
            borderBottom: '2px solid #8b5cf6',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}>
            <span>Code Review</span>
            <span style={{
              background: '#8b5cf6',
              color: '#fff',
              borderRadius: 10,
              padding: '1px 7px',
              fontSize: 11,
              fontWeight: 700,
            }}>
              {columns.review.length + columns.review_ok.length}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 12 }}>
            <SubColumn
              title="Aguardando"
              issues={columns.review}
              color="#8b5cf6"
              columnKey="review"
            />
            <SubColumn
              title="Aprovado ✓"
              issues={columns.review_ok}
              color="#22c55e"
              columnKey="review_ok"
            />
          </div>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// App principal
// ---------------------------------------------------------------------------

export default function CrewFlow() {
  const api = useAppApi()
  const [columns, setColumns] = useState<Columns>(emptyColumns())
  const [squadName, setSquadName] = useState<string>('KiroCrew Flow')
  const [project, setProject] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [isMock, setIsMock] = useState(false)
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)
  const [dispatchingKey, setDispatchingKey] = useState<string | null>(null)

  const load = useCallback(() => {
    return api
      .get('/api/apps/kirocrew-flow/issues')
      .then((d: ApiResponse) => {
        setColumns(d.columns ?? emptyColumns())
        setSquadName(d.squad_name || 'KiroCrew Flow')
        setProject(d.project || '')
        setLastUpdate(new Date())
        setError(null)
        setIsMock(false)
      })
      .catch((err: unknown) => {
        const msg = String(err)
        // 404 = backend.routes não disponível para apps de terceiros (issue #170)
        if (msg.includes('404') || msg.includes('not found')) {
          const mock = MOCK_RESPONSE
          setColumns(mock.columns ?? emptyColumns())
          setSquadName(mock.squad_name || 'KiroCrew Flow')
          setProject(mock.project || '')
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

  const title = project ? `Flow - ${squadName} / ${project.split('/').pop()}` : squadName

  const totalActive = (
    columns.spec.length +
    columns.ready.length +
    columns.todo.length +
    columns.dev.length +
    columns.review.length +
    columns.review_ok.length +
    columns.blocked.length
  )

  return (
    <div style={{ padding: '20px 24px', maxWidth: 1400 }}>
      <PageHeader
        title={title}
        subtitle={
          loading
            ? 'Carregando…'
            : error
            ? `Erro: ${error}`
            : isMock
            ? '⚠️ Modo demo — backend indisponível'
            : lastUpdate
            ? `${totalActive} issues ativas · atualizado ${lastUpdate.toLocaleTimeString()}`
            : ''
        }
        actions={
          <div style={{ display: 'flex', gap: 8 }}>
            <Btn size="sm" variant="secondary" disabled>
              Rules
            </Btn>
            <Btn size="sm" variant="secondary" disabled>
              Configurações
            </Btn>
            <Btn size="sm" variant="secondary" onClick={load} disabled={loading}>
              ↻ Atualizar
            </Btn>
          </div>
        }
      />

      {/* Seções de espera humana */}
      <div style={{ display: 'flex', gap: 16, marginTop: 20, flexWrap: 'wrap' }}>
        <WaitingSection
          title="Aguardando SPEC"
          subtitle="PM especificando"
          issues={columns.spec}
          accentColor="#f59e0b"
        />
        <WaitingSection
          title="Aguardando definição de produto/TL"
          subtitle="Spec pronta, aguardando priorização"
          issues={columns.ready}
          accentColor="#fbbf24"
        />
      </div>

      {/* Seção de agentes */}
      <AgentsPanel
        columns={columns}
        onDispatch={handleDispatch}
        dispatchingKey={dispatchingKey}
      />

      {/* Coluna blocked separada abaixo */}
      {columns.blocked.length > 0 && (
        <div style={{
          marginTop: 20,
          border: '1px solid #dc262633',
          borderRadius: 10,
          padding: '14px 16px',
          background: '#dc262608',
        }}>
          <div style={{
            fontWeight: 700,
            fontSize: 13,
            color: '#dc2626',
            marginBottom: 10,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}>
            🔴 Bloqueadas
            <span style={{
              background: '#dc2626',
              color: '#fff',
              borderRadius: 10,
              padding: '1px 7px',
              fontSize: 11,
            }}>
              {columns.blocked.length}
            </span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {columns.blocked.map((issue) => (
              <div key={`${issue.repo}#${issue.number}`} style={{ minWidth: 200, flex: '0 0 auto', maxWidth: 280 }}>
                <IssueCard issue={issue} />
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
