import { useState, type FormEvent } from 'react'
import { api, type AskResponse, type Asset, type Candle, type NewsItem } from './api'
import { AnswerText } from './components/AnswerText'
import { PriceChart } from './components/PriceChart'
import { TraceView } from './components/TraceView'
import { fmtTime, localInputToIso, nowInput } from './format'

const SECTIONS: Array<[key: string, title: string]> = [
  ['what_happened', 'Що сталося'],
  ['likely_causes', 'Ймовірні причини'],
  ['similar_episodes', 'Схожі епізоди'],
  ['forecast', 'Прогноз'],
]
const WINDOWS = [
  [24, '24 год'],
  [72, '3 дні'],
  [168, '7 днів'],
  [720, '30 днів'],
] as const
const DISCLAIMER = 'Не є фінансовою порадою.'

export function QueryScreen() {
  const [question, setQuestion] = useState('Чому змінилась ціна за останню добу?')
  const [asset, setAsset] = useState<Asset>('BTC')
  const [asOf, setAsOf] = useState(nowInput())
  const [windowHours, setWindowHours] = useState(24)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AskResponse | null>(null)
  const [candles, setCandles] = useState<Candle[]>([])
  const [news, setNews] = useState<NewsItem[]>([])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
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
  const cardMetrics = result?.metrics.filter((m) => m.id.startsWith('m') && whatHappened.includes(`[${m.id}]`)) ?? []
  const body = result ? result.answer.replace(DISCLAIMER, '').trim() : ''
  const hasSections = result && SECTIONS.some(([k]) => result.sections[k])

  return (
    <div className="query">
      <form className="card ask" onSubmit={submit}>
        <label className="field grow">
          <span>Запитання</span>
          <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={2} required minLength={3} />
        </label>
        <div className="row">
          <div className="field">
            <span>Актив</span>
            <div className="toggle" role="radiogroup">
              {(['BTC', 'ETH'] as Asset[]).map((a) => (
                <button type="button" key={a} role="radio" aria-checked={asset === a}
                        className={asset === a ? 'on' : ''} onClick={() => setAsset(a)}>{a}</button>
              ))}
            </div>
          </div>
          <label className="field">
            <span>Станом на (UTC)</span>
            <input type="datetime-local" value={asOf} max={nowInput()} onChange={(e) => setAsOf(e.target.value)} />
          </label>
          <label className="field">
            <span>Вікно новин</span>
            <select value={windowHours} onChange={(e) => setWindowHours(Number(e.target.value))}>
              {WINDOWS.map(([h, label]) => <option key={h} value={h}>{label}</option>)}
            </select>
          </label>
          <button className="primary" type="submit" disabled={loading}>{loading ? 'Аналізую…' : 'Запитати'}</button>
        </div>
      </form>

      {error && <div className="card error-box" role="alert">{error}</div>}
      {loading && <div className="card muted">Збираю ринкові дані, шукаю новини, перевіряю відповідь… це може тривати до хвилини.</div>}

      {result && (
        <div className="result">
          <div className="main-col">
            <div className={`badge ${result.verification.ok ? 'ok' : 'bad'}`}>
              {result.verification.ok ? '✓ Перевірку пройдено' : '✗ Перевірку не пройдено'}
              <span className="muted"> · станом на {fmtTime(result.as_of)} UTC · {result.asset} · {result.intent}</span>
            </div>
            {!result.verification.ok && !!result.verification.errors.length && (
              <ul className="card error-box">
                {result.verification.errors.map((e, i) => <li key={i}><code>{e.check}</code> {e.message}</li>)}
              </ul>
            )}

            {cardMetrics.length > 0 && (
              <div className="metric-cards">
                {cardMetrics.map((m) => (
                  <div className="metric" key={m.id} title={m.description}>
                    <div className="value">{m.display}</div>
                    <div className="label">{m.description}</div>
                    <div className="id">{m.id}</div>
                  </div>
                ))}
              </div>
            )}

            {hasSections ? (
              SECTIONS.filter(([k]) => result.sections[k]).map(([k, title]) => (
                <section className="card" key={k}>
                  <h3>{title}</h3>
                  <AnswerText text={result.sections[k].replace(DISCLAIMER, '')} sources={result.sources} metrics={result.metrics} />
                </section>
              ))
            ) : (
              <section className="card">
                <AnswerText text={body} sources={result.sources} metrics={result.metrics} />
              </section>
            )}
            <p className="disclaimer">{DISCLAIMER}</p>
            <TraceView queryId={result.query_id} />
          </div>

          <aside className="side-col">
            <section className="card">
              <h3>Ціна {asset}</h3>
              <PriceChart candles={candles} news={news} cited={result.sources} asOf={result.as_of} />
            </section>
            <section className="card">
              <h3>Джерела</h3>
              {result.sources.length === 0 && <p className="muted">Відповідь не посилається на новини.</p>}
              <ol className="sources">
                {result.sources.map((s) => (
                  <li key={s.id} id={`src-${s.id}`}>
                    <a href={s.url} target="_blank" rel="noreferrer">{s.title}</a>
                    <div className="muted small">
                      {s.source} · {fmtTime(s.published_at)} UTC{s.score !== null && ` · вага ${s.score.toFixed(4)}`}
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          </aside>
        </div>
      )}
    </div>
  )
}
