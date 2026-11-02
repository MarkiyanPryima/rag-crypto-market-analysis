import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { api, type AskResponse, type Asset, type Candle, type Metric, type NewsItem } from './api'
import { AnswerText } from './components/AnswerText'
import { PriceChart } from './components/PriceChart'
import { TraceView } from './components/TraceView'
import { fmtTime, localInputToIso, nowInput, relativeTo } from './format'
import {
  IconAlert, IconArrowDown, IconArrowUp, IconHistory, IconInfo, IconLink, IconNews, IconSearch, IconShield, IconSpark, IconTarget,
} from './icons'

const SECTIONS: Array<{ key: string; title: string; cls: string; icon: ReactNode; ico: string }> = [
  { key: 'what_happened', title: 'Що сталося', cls: '', icon: <IconPulseSmall />, ico: '' },
  { key: 'likely_causes', title: 'Ймовірні причини', cls: 'causes', icon: <IconSearch />, ico: 'orange' },
  { key: 'similar_episodes', title: 'Схожі епізоди', cls: 'episodes', icon: <IconHistory />, ico: 'aqua' },
  { key: 'forecast', title: 'Прогноз', cls: 'forecast', icon: <IconTarget />, ico: 'violet' },
]
const WINDOWS = [[24, '24 год'], [72, '3 дні'], [168, '7 днів'], [720, '30 днів']] as const
const EXAMPLES = [
  'Чому змінилась ціна за останню добу?',
  'Що вплинуло на ринок за останні 3 дні?',
  'Які новини збіглися з найрізкішим рухом цього тижня?',
  'Який прогноз волатильності на завтра?',
]
const STEPS = ['Маршрутизація', 'Ринкові дані', 'Пошук новин', 'Генерація', 'Перевірка фактів']
const DISCLAIMER = 'Не є фінансовою порадою.'

function IconPulseSmall() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M3 12h4l3-8 4 16 3-8h4" /></svg>
}

function Progress() {
  // The API answers in one response; steps advance on a timer to show what is happening.
  const [i, setI] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setI((v) => Math.min(v + 1, STEPS.length - 1)), 3500)
    return () => clearInterval(id)
  }, [])
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="progress">
        {STEPS.map((s, k) => (
          <span key={s} className={`step ${k < i ? 'done' : k === i ? 'active' : ''}`}>
            {k < i ? '✓' : k === i ? <span className="spinner" /> : null}{s}
          </span>
        ))}
      </div>
      <div style={{ marginTop: 18 }}>
        <div className="skeleton" style={{ width: '92%' }} />
        <div className="skeleton" style={{ width: '78%', marginTop: 10 }} />
        <div className="skeleton" style={{ width: '64%', marginTop: 10 }} />
      </div>
    </div>
  )
}

// Short Ukrainian card titles; the tool's full description stays in the tooltip.
const KPI_LABEL: Record<string, string> = {
  price_end: 'Ціна наприкінці вікна',
  price_change_pct: 'Зміна ціни за вікно',
  sharpest_hour_pct: 'Найрізкіша година',
  volatility_hourly_pct: 'Волатильність (погодинна)',
  volume_vs_30d: 'Обсяг відносно 30-денної норми',
  taker_buy_share_pct: 'Частка агресивних покупок',
  funding_rate_pct: 'Ставка фінансування',
  volatility_percentile: 'Перцентиль волатильності за рік',
  largest_drop_days: 'Найбільше падіння за, днів',
  largest_rise_days: 'Найбільше зростання за, днів',
  price_start: 'Ціна на початку вікна',
}
const KPI_ORDER = Object.keys(KPI_LABEL)
const SIGNED = new Set(['price_change_pct', 'sharpest_hour_pct', 'funding_change_pp'])

function Kpi({ m }: { m: Metric }) {
  const signed = SIGNED.has(m.name) && m.value !== null && m.value !== 0
  const up = (m.value ?? 0) > 0
  const [value, unit] = m.display.endsWith(' USDT') ? [m.display.slice(0, -5), 'USDT'] : [m.display, '']
  return (
    <div className="kpi" title={m.description}>
      <span className="ref">{m.id}</span>
      <div className="label">{KPI_LABEL[m.name] ?? m.name}</div>
      <div className="value">
        {signed && <span className={`trend ${up ? 'up' : 'down'}`} aria-label={up ? 'зростання' : 'падіння'}>{up ? <IconArrowUp /> : <IconArrowDown />}</span>}
        {value}{unit && <span className="unit">{unit}</span>}
      </div>
    </div>
  )
}

export function QueryScreen() {
  const [question, setQuestion] = useState(EXAMPLES[0])
  const [asset, setAsset] = useState<Asset>('BTC')
  const [asOf, setAsOf] = useState(nowInput())
  const [windowHours, setWindowHours] = useState(24)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AskResponse | null>(null)
  const [candles, setCandles] = useState<Candle[]>([])
  const [news, setNews] = useState<NewsItem[]>([])

  const submit = async (e?: FormEvent) => {
    e?.preventDefault()
    if (question.trim().length < 3) return
    setLoading(true)
    setError(null)
    setResult(null)
    const asOfIso = localInputToIso(asOf) ?? new Date().toISOString()
    const start = new Date(new Date(asOfIso).getTime() - Math.max(windowHours, 48) * 3_600_000).toISOString()
    try {
      const [answer, prices, items] = await Promise.all([
        api.ask({ question, asset, as_of: asOfIso, window_hours: windowHours }),
        api.prices(asset, start, asOfIso),
        api.news(start, asOfIso, asset),
      ])
      setResult(answer)
      setCandles(prices)
      setNews(items)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const whatHappened = result?.sections.what_happened ?? ''
  const kpis = (result?.metrics ?? [])
    .filter((m) => m.id.startsWith('m') && KPI_LABEL[m.name] && whatHappened.includes(`[${m.id}]`))
    .filter((m, _, all) => m.name !== 'price_start' || !all.some((x) => x.name === 'price_end'))
    .sort((a, b) => KPI_ORDER.indexOf(a.name) - KPI_ORDER.indexOf(b.name))
    .slice(0, 6)
  const shown = result ? SECTIONS.filter((s) => result.sections[s.key]) : []
  const body = result ? result.answer.replace(DISCLAIMER, '').trim() : ''
  const maxScore = Math.max(...(result?.sources.map((s) => s.score ?? 0) ?? [0]), 1e-9)

  return (
    <div>
      <form className="card hero" onSubmit={submit}>
        <h1>Що відбувається на ринку?</h1>
        <p className="lead">Пояснення рухів ціни на основі новин і ринкових даних. Кожне число перевіряється кодом.</p>
        <label className="ask-box">
          <IconSpark />
          <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={1} required minLength={3}
                    aria-label="Запитання" placeholder="Наприклад: чому BTC впав сьогодні?"
                    onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void submit() } }} />
        </label>
        <div className="controls">
          <div className="field">
            Актив
            <div className="segmented" role="radiogroup" aria-label="Актив">
              {(['BTC', 'ETH'] as Asset[]).map((a) => (
                <button type="button" key={a} role="radio" aria-checked={asset === a} className={asset === a ? 'on' : ''} onClick={() => setAsset(a)}>
                  <span className={`coin ${a.toLowerCase()}`}>{a === 'BTC' ? '₿' : 'Ξ'}</span>{a}
                </button>
              ))}
            </div>
          </div>
          <label className="field">
            Станом на (UTC)
            <input className="input" type="datetime-local" value={asOf} max={nowInput()} onChange={(e) => setAsOf(e.target.value)} />
          </label>
          <label className="field">
            Вікно новин
            <select className="input" value={windowHours} onChange={(e) => setWindowHours(Number(e.target.value))}>
              {WINDOWS.map(([h, label]) => <option key={h} value={h}>{label}</option>)}
            </select>
          </label>
          <span className="grow" />
          <button className="btn-primary" type="submit" disabled={loading}>
            {loading ? <span className="spinner" /> : <IconSearch />}{loading ? 'Аналізую…' : 'Запитати'}
          </button>
        </div>
        <div className="examples">
          <span>Спробуйте:</span>
          {EXAMPLES.map((ex) => <button type="button" key={ex} className="chip" onClick={() => setQuestion(ex)}>{ex}</button>)}
        </div>
      </form>

      {error && <div className="error-box" role="alert"><IconAlert />{error}</div>}
      {loading && <Progress />}

      {result && (
        <div className="result">
          <div className="stack">
            <div className="result-head">
              {result.refused ? (
                <span className="badge neutral"><IconInfo />{result.intent === 'unsupported' ? 'Поза межами системи' : 'Відповідь не сформовано'}</span>
              ) : result.verification.ok ? (
                <span className="badge ok"><IconShield />Перевірку фактів пройдено</span>
              ) : (
                <span className="badge bad"><IconAlert />Перевірку не пройдено</span>
              )}
              <span className="meta-chip">станом на {fmtTime(result.as_of)} UTC</span>
              {result.asset && <span className="meta-chip"><span className={`coin ${result.asset.toLowerCase()}`}>{result.asset === 'BTC' ? '₿' : 'Ξ'}</span>{result.asset}</span>}
              {result.intent && result.intent !== 'unsupported' && <span className="meta-chip">{result.intent}</span>}
            </div>
            {!result.refused && !result.verification.ok && !!result.verification.errors.length && (
              <ul className="error-list">{result.verification.errors.map((e, i) => <li key={i}><code>{e.check}</code> {e.message}</li>)}</ul>
            )}

            {kpis.length > 0 && <div className="kpis">{kpis.map((m) => <Kpi key={m.id} m={m} />)}</div>}

            {shown.length ? (
              shown.map((s) => (
                <section className={`card section ${s.cls}`} key={s.key}>
                  <h3 className="card-title"><span className={`ico ${s.ico}`}>{s.icon}</span>{s.title}</h3>
                  <AnswerText text={result.sections[s.key].replace(DISCLAIMER, '')} sources={result.sources} metrics={result.metrics} />
                </section>
              ))
            ) : (
              <section className="card"><AnswerText text={body} sources={result.sources} metrics={result.metrics} /></section>
            )}
            <p className="disclaimer"><IconInfo />{DISCLAIMER}</p>
            {result.query_id > 0 && <TraceView queryId={result.query_id} />}
          </div>

          <aside className="stack">
            <section className="card">
              <h3 className="card-title"><span className="ico">{result.asset === 'ETH' ? 'Ξ' : '₿'}</span>Ціна {result.asset ?? asset}</h3>
              <PriceChart candles={candles} news={news} cited={result.sources} asOf={result.as_of} />
            </section>
            <section className="card">
              <h3 className="card-title"><span className="ico orange"><IconNews /></span>Джерела</h3>
              {result.sources.length === 0 ? (
                <div className="empty"><IconInfo />Відповідь не посилається на новини.</div>
              ) : (
                <ol className="sources">
                  {result.sources.map((s, i) => (
                    <li key={s.id} id={`src-${s.id}`} className="source">
                      <span className="n">{i + 1}</span>
                      <div>
                        <a href={s.url} target="_blank" rel="noreferrer">{s.title} <IconLink width={13} height={13} /></a>
                        <div className="meta">
                          <span className={`src-badge ${s.source}`}>{s.source}</span>
                          <span className="num">{fmtTime(s.published_at)} UTC</span>
                          <span>· {relativeTo(s.published_at, result.as_of)}</span>
                          {s.score !== null && (
                            <span title={`Вага в пошуку: ${s.score.toFixed(5)}`}>
                              · <span className="weight"><i style={{ width: `${Math.round((s.score / maxScore) * 100)}%` }} /></span>
                            </span>
                          )}
                        </div>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </section>
          </aside>
        </div>
      )}
    </div>
  )
}
