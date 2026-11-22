// Typed client for the Crypto RAG API. The UI never touches the database.

export type Asset = 'BTC' | 'ETH'

export interface Source {
  id: string
  title: string
  url: string
  source: string
  published_at: string
  score: number | null
}

export interface Metric {
  id: string
  name: string
  value: number | null
  display: string
  unit: string
  description: string
}

export interface VerificationError {
  check: string
  message: string
  token: string | null
}

export interface AskResponse {
  query_id: number
  as_of: string
  asset: string | null
  intent: string | null
  answer: string
  sections: Record<string, string>
  sources: Source[]
  metrics: Metric[]
  verification: { ok: boolean; errors: VerificationError[] }
  refused: boolean
}

export interface Candle {
  ts: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface NewsItem {
  id: string
  title: string
  url: string
  source: string
  channel: string
  published_at: string
  topic: string | null
  tone: string | null
}

export interface Trace {
  id: number
  question: string
  asset: string | null
  as_of: string
  intent: string | null
  routing: { output?: Record<string, unknown>; asset_used?: string; window_hours?: number } | null
  tool_calls: Array<Record<string, unknown> & { name: string; ms?: number; error?: string }>
  retrieved: Array<{
    id: string
    title: string
    source: string
    published_at: string
    rank_vec?: number | null
    rank_fts?: number | null
    rrf?: number
    decay?: number
    score?: number
    offset_hours?: number
  }>
  attempts: Array<{ event: string; attempt?: number; ok?: boolean; errors?: VerificationError[] }>
  verification: { ok: boolean; errors: VerificationError[] }
  answer: string | null
}

export interface Stats {
  data: {
    candles: Array<{ symbol: string; n: number; first: string; last: string }>
    articles_by_month: Array<{ month: string; source: string; channel: string; n: number }>
    totals: Record<string, number>
  }
  topic_reaction: Array<{
    topic: string
    articles: number
    avg_move_6h_pct: number
    avg_abs_move_6h_pct: number
    vol_ratio_after_before: number | null
    avg_tone: number
  }>
  forecasts: Array<{
    symbol: string
    kind: string
    n: number
    latest_date: string
    latest_value: number
    model_version: string
  }>
  llm_usage: Array<{
    purpose: string
    model: string
    calls: number
    input_tokens: number
    output_tokens: number
    cost_usd: number | null
  }>
}

export interface Health {
  status: string
  database: string
  last_candle: string | null
  last_article: string | null
  last_fetch: string | null
  openai_configured: boolean
  guardian_configured: boolean
}

export type AnnotationTask = 'relevance' | 'claims' | 'labels'

export interface AnnotationItem<P = Record<string, unknown>, L = Record<string, unknown>> {
  item_id: string
  payload: P
  label: L | null
}

export interface ArticleRef {
  id: string
  source: string
  channel?: string
  title: string
  standfirst: string
  body: string
  url: string
  published_at: string
}

export interface RelevancePayload {
  question_id: string
  question: string
  asset: Asset
  move_pct: number
  window_start: string
  window_end: string
  article: ArticleRef
}

export interface ClaimsPayload {
  question_id: string
  question: string
  as_of: string
  answer: string
  context: string
  claims: string[]
}

export type LabelsPayload = Omit<ArticleRef, 'id' | 'channel'>

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    throw new ApiError(0, 'Сервер API недоступний. Перевірте, що сервіс api запущено.')
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* body was not JSON */
    }
    throw new ApiError(res.status, humanize(res.status, detail))
  }
  return res.json() as Promise<T>
}

function humanize(status: number, detail: string): string {
  if (status === 503) return `Сервіс тимчасово недоступний: ${detail}`
  if (status === 422) return `Некоректний запит: ${detail}`
  if (status === 404) return `Не знайдено: ${detail}`
  if (status >= 500) return `Помилка сервера (${status}): ${detail}`
  return `${status}: ${detail}`
}

const qs = (params: Record<string, string | undefined>) =>
  new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined) as [string, string][]).toString()

export const api = {
  ask: (body: { question: string; asset?: Asset; as_of?: string; window_hours?: number }) =>
    request<AskResponse>('/ask', { method: 'POST', body: JSON.stringify(body) }),
  trace: (id: number) => request<Trace>(`/trace/${id}`),
  stats: () => request<Stats>('/stats'),
  health: () => request<Health>('/health'),
  prices: (asset: Asset, start: string, asOf: string) =>
    request<Candle[]>(`/prices?${qs({ asset, start, as_of: asOf })}`),
  news: (start: string, asOf: string, asset?: Asset) =>
    request<NewsItem[]>(`/news?${qs({ start, as_of: asOf, asset, limit: '100' })}`),
  annotationProgress: () => request<Record<string, { total: number; done: number }>>('/annotations'),
  annotations: <P, L>(task: AnnotationTask) => request<AnnotationItem<P, L>[]>(`/annotations/${task}`),
  saveAnnotation: (task: AnnotationTask, itemId: string, label: Record<string, unknown>) =>
    request<{ item_id: string }>(`/annotations/${task}/${itemId.split('/').map(encodeURIComponent).join('/')}`, {
      method: 'PUT',
      body: JSON.stringify({ label }),
    }),
}
