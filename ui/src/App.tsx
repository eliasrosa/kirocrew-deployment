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
  briefing: Issue[]
  planning_specs: Issue[]
  planning_review: Issue[]
  develop_waiting: Issue[]
  develop_running: Issue[]
  review_waiting: Issue[]
  review_approved: Issue[]
  review_refused: Issue[]
  qa_waiting: Issue[]
  qa_testing: Issue[]
  qa_approved: Issue[]
  qa_refused: Issue[]
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
    briefing: [],
    planning_specs: [],
    planning_review: [],
    develop_waiting: [],
    develop_running: [],
    review_waiting: [],
    review_approved: [],
    review_refused: [],
    qa_waiting: [],
    qa_testing: [],
    qa_approved: [],
    qa_refused: [],
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
    briefing: [
      { number: 95, title: 'Exemplo: demanda sendo especificada', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 240, labels: ['flow:briefing'], blocked: false, running: false },
    ],
    planning_specs: [
      { number: 97, title: 'Exemplo: dev montando spec/critérios', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 120, labels: ['flow:planning-specs'], blocked: false, running: false },
    ],
    planning_review: [],
    develop_waiting: [
      { number: 99, title: 'Exemplo: aguardando agente', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 45, labels: ['flow:develop-waiting'], blocked: false, running: false },
    ],
    develop_running: [
      { number: 100, title: 'Exemplo: agente implementando', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 120, labels: ['flow:develop-running'], blocked: false, running: true },
    ],
    review_waiting: [
      { number: 101, title: 'Exemplo: PR aguardando reviewer', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 30, labels: ['flow:review-waiting'], blocked: false, running: false },
    ],
    review_approved: [
      { number: 103, title: 'Exemplo: reviewer aprovou — aguardando merge', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 15, labels: ['flow:review-approved'], blocked: false, running: false },
    ],
    review_refused: [],
    qa_waiting: [],
    qa_testing: [],
    qa_approved: [],
    qa_refused: [],
    done: [],
    blocked: [
      { number: 102, title: 'Exemplo: issue bloqueada', repo: 'eliasrosa/kirocrew-flow', url: '', age_min: 240, labels: ['flow:blocked'], blocked: true, running: false },
    ],
  },
}

// ---------------------------------------------------------------------------
// IssueCard
// ---------------------------------------------------------------------------

interface IssueCardProps {
  issue: Issue
  showDispatch?: boolean
  onDispatch?: (repo: string, number: number | string) => Promise<void>
  dispatching?: boolean
  showQaButtons?: boolean
  onQaFail?: (repo: string, number: number | string) => Promise<void>
  onQaApprove?: (repo: string, number: number | string) => Promise<void>
  qaActioning?: string  // 'fail' | 'approve' | undefined
}

function IssueCard({ issue, showDispatch, onDispatch, dispatching, showQaButtons, onQaFail, onQaApprove, qaActioning }: IssueCardProps) {
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
        {showQaButtons && (onQaApprove || onQaFail) && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, flexShrink: 0 }}>
            {onQaApprove && (
              <Btn
                size="sm"
                variant="primary"
                disabled={!!qaActioning}
                onClick={() => onQaApprove(issue.repo, issue.number)}
                style={{ fontSize: 11, background: '#22c55e', borderColor: '#16a34a' }}
              >
                {qaActioning === 'approve' ? '...' : '✓ Aprovar'}
              </Btn>
            )}
            {onQaFail && (
              <Btn
                size="sm"
                variant="secondary"
                disabled={!!qaActioning}
                onClick={() => onQaFail(issue.repo, issue.number)}
                style={{ fontSize: 11, borderColor: '#9333ea', color: '#9333ea' }}
              >
                {qaActioning === 'fail' ? '...' : '✗ Reprovar'}
              </Btn>
            )}
          </div>
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
// SubColumn — coluna dentro de um painel
// ---------------------------------------------------------------------------

interface SubColumnProps {
  title: string
  issues: Issue[]
  color: string
  showDispatch?: boolean
  onDispatch?: (repo: string, number: number | string) => Promise<void>
  dispatchingKey?: string
  showQaButtons?: boolean
  onQaFail?: (repo: string, number: number | string) => Promise<void>
  onQaApprove?: (repo: string, number: number | string) => Promise<void>
  qaActioningKey?: string
}

function SubColumn({ title, issues, color, showDispatch, onDispatch, dispatchingKey, showQaButtons, onQaFail, onQaApprove, qaActioningKey }: SubColumnProps) {
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
              showQaButtons={showQaButtons}
              onQaFail={onQaFail}
              onQaApprove={onQaApprove}
              qaActioning={qaActioningKey?.startsWith(`${key}:`) ? qaActioningKey.split(':').pop() : undefined}
            />
          )
        })
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// PlanningSection — fase de planning (briefing → planning-review)
// ---------------------------------------------------------------------------

interface PlanningSectionProps {
  columns: Columns
  accentColor: string
}

function PlanningSection({ columns, accentColor }: PlanningSectionProps) {
  const total = columns.briefing.length + columns.planning_specs.length + columns.planning_review.length
  return (
    <div style={{
      border: `1px solid ${accentColor}33`,
      borderRadius: 10,
      padding: '14px 16px',
      background: `${accentColor}08`,
      flex: 1,
      minWidth: 260,
    }}>
      <div style={{ marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{ fontWeight: 700, fontSize: 13, color: accentColor }}>Planning</div>
        <span style={{
          background: accentColor + '33',
          color: accentColor,
          borderRadius: 10,
          padding: '1px 7px',
          fontSize: 11,
          fontWeight: 700,
        }}>{total}</span>
      </div>
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <SubColumn title="Briefing" issues={columns.briefing} color="#f59e0b" />
        <SubColumn title="Specs" issues={columns.planning_specs} color="#fbbf24" />
        <SubColumn title="Revisão TL" issues={columns.planning_review} color="#d97706" />
      </div>
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
  const refusedCount = columns.review_refused.length + columns.qa_refused.length
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
              {columns.develop_waiting.length + columns.develop_running.length}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 12 }}>
            <SubColumn
              title="Aguardando"
              issues={columns.develop_waiting}
              color="#16a34a"
              showDispatch
              onDispatch={onDispatch}
              dispatchingKey={dispatchingKey ?? undefined}
            />
            <SubColumn
              title="Implementando"
              issues={columns.develop_running}
              color="#2563eb"
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
              {columns.review_waiting.length + columns.review_approved.length}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 12 }}>
            <SubColumn
              title="Aguardando"
              issues={columns.review_waiting}
              color="#8b5cf6"
            />
            <SubColumn
              title="Aprovado ✓"
              issues={columns.review_approved}
              color="#22c55e"
            />
          </div>
        </div>

        {/* Painel: QA */}
        <div style={{ flex: 1, minWidth: 300 }}>
          <div style={{
            fontWeight: 700,
            fontSize: 13,
            marginBottom: 12,
            paddingBottom: 6,
            borderBottom: '2px solid #0ea5e9',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}>
            <span>QA</span>
            <span style={{
              background: '#0ea5e9',
              color: '#fff',
              borderRadius: 10,
              padding: '1px 7px',
              fontSize: 11,
              fontWeight: 700,
            }}>
              {columns.qa_waiting.length + columns.qa_testing.length + columns.qa_approved.length}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 12 }}>
            <SubColumn title="Aguardando" issues={columns.qa_waiting} color="#0ea5e9" />
            <SubColumn
              title="Testando"
              issues={columns.qa_testing}
              color="#38bdf8"
              showQaButtons
              onQaFail={handleQaFail}
              onQaApprove={handleQaApprove}
              qaActioningKey={qaActioningKey ?? undefined}
            />
            <SubColumn title="Aprovado ✓" issues={columns.qa_approved} color="#22c55e" />
          </div>
        </div>
      </div>

      {/* Gates humanos reprovados — aviso em destaque */}
      {refusedCount > 0 && (
        <div style={{
          marginTop: 16,
          border: '1px solid #9333ea33',
          borderRadius: 8,
          padding: '10px 14px',
          background: '#9333ea08',
        }}>
          <div style={{ fontWeight: 700, fontSize: 12, color: '#9333ea', marginBottom: 8 }}>
            🚫 Gates humanos — aguardando decisão manual do TL/dev
          </div>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            {columns.review_refused.length > 0 && (
              <SubColumn title="Review reprovado" issues={columns.review_refused} color="#9333ea" />
            )}
            {columns.qa_refused.length > 0 && (
              <SubColumn title="QA reprovado" issues={columns.qa_refused} color="#9333ea" />
            )}
          </div>
        </div>
      )}
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
  const [qaActioningKey, setQaActioningKey] = useState<string | null>(null)

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

  const handleQaFail = useCallback(
    async (repo: string, number: number | string) => {
      const key = `${repo}#${number}:fail`
      setQaActioningKey(key)
      try {
        await api.post('/api/apps/kirocrew-flow/qa-fail', { repo, number: Number(number) })
        await load()
      } catch (err) {
        console.error('qa-fail failed:', err)
      } finally {
        setQaActioningKey(null)
      }
    },
    [api, load],
  )

  const handleQaApprove = useCallback(
    async (repo: string, number: number | string) => {
      const key = `${repo}#${number}:approve`
      setQaActioningKey(key)
      try {
        await api.post('/api/apps/kirocrew-flow/qa-approve', { repo, number: Number(number) })
        await load()
      } catch (err) {
        console.error('qa-approve failed:', err)
      } finally {
        setQaActioningKey(null)
      }
    },
    [api, load],
  )

  const title = project ? `Flow - ${squadName} / ${project.split('/').pop()}` : squadName

  const totalActive = (
    columns.briefing.length +
    columns.planning_specs.length +
    columns.planning_review.length +
    columns.develop_waiting.length +
    columns.develop_running.length +
    columns.review_waiting.length +
    columns.review_approved.length +
    columns.review_refused.length +
    columns.qa_waiting.length +
    columns.qa_testing.length +
    columns.qa_approved.length +
    columns.qa_refused.length +
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

      {/* Fase de planning */}
      <div style={{ display: 'flex', gap: 16, marginTop: 20, flexWrap: 'wrap' }}>
        <PlanningSection columns={columns} accentColor="#f59e0b" />
      </div>

      {/* Seção de agentes (dev + review + QA) */}
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
