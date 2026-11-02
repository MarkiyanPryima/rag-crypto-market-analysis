import { useState } from 'react'
import { api, type Trace } from '../api'
import { fmtTime, num } from '../format'
import { IconAlert, IconChevron, IconPulse, IconShield } from '../icons'

type Sub = 'routing' | 'tools' | 'chunks' | 'verify'
const SUBS: Array<[Sub, string]> = [
  ['routing', 'Маршрутизація'],
  ['tools', 'Інструменти'],
  ['chunks', 'Знайдені фрагменти'],
  ['verify', 'Перевірка'],
]

const Rank = ({ v }: { v: number | null | undefined }) => <span className={`rank ${v ? '' : 'none'}`}>{v ?? '—'}</span>

/** "Як отримано відповідь": routing, tool calls, retrieved chunks with ranks, verification attempts. */
export function TraceView({ queryId }: { queryId: number }) {
  const [trace, setTrace] = useState<Trace | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [sub, setSub] = useState<Sub>('routing')

  const toggle = async () => {
    setOpen(!open)
    if (!trace && !open) {
      try {
        setTrace(await api.trace(queryId))
      } catch (e) {
        setError((e as Error).message)
      }
    }
  }

  return (
    <section className="card">
      <button className="trace-toggle" onClick={toggle} aria-expanded={open}>
        <span className="card-title" style={{ margin: 0 }}>
          <span className="ico violet"><IconPulse /></span>Як отримано відповідь
        </span>
        <span className="muted small">запит #{queryId}</span>
        <span style={{ flex: 1 }} />
        <IconChevron className="chev" />
      </button>
      {open && error && <div className="error-box"><IconAlert />{error}</div>}
      {open && !trace && !error && <div style={{ marginTop: 16 }}><div className="skeleton" /><div className="skeleton" style={{ width: '70%', marginTop: 8 }} /></div>}
      {open && trace && (
        <>
          <div className="subtabs" role="tablist">
            {SUBS.map(([k, label]) => (
              <button key={k} role="tab" aria-selected={sub === k} className={sub === k ? 'on' : ''} onClick={() => setSub(k)}>
                {label}
                {k === 'tools' && ` (${trace.tool_calls.length})`}
                {k === 'chunks' && ` (${trace.retrieved.length})`}
              </button>
            ))}
          </div>

          {sub === 'routing' && (
            <>
              <dl className="kv">
                <div><dt>Намір</dt><dd>{trace.intent ?? '—'}</dd></div>
                <div><dt>Актив</dt><dd>{trace.routing?.asset_used ?? trace.asset ?? '—'}</dd></div>
                <div><dt>Вікно</dt><dd>{trace.routing?.window_hours ?? '—'} год</dd></div>
                <div><dt>Станом на</dt><dd className="num">{fmtTime(trace.as_of)} UTC</dd></div>
              </dl>
              {typeof trace.routing?.output?.reason === 'string' && <p className="muted small" style={{ marginBottom: 0 }}>{trace.routing.output.reason}</p>}
            </>
          )}

          {sub === 'tools' && (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Інструмент</th><th>Аргументи</th><th className="r">мс</th><th>Примітка</th></tr></thead>
                <tbody>
                  {trace.tool_calls.map((c, i) => (
                    <tr key={i}>
                      <td><code>{c.name}</code></td>
                      <td><code>{JSON.stringify(c.args)}</code></td>
                      <td className="r">{c.ms ?? '—'}</td>
                      <td>
                        {c.error ? <span className="bar-neg">{String(c.error)}</span>
                          : c.ignored_model_as_of ? `дату моделі (${String(c.ignored_model_as_of)}) проігноровано` : ''}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {sub === 'chunks' && (
            trace.retrieved.length === 0 ? <div className="empty">Пошук не повернув жодного фрагмента.</div> : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>id</th><th>Заголовок</th><th>Дата (UTC)</th><th className="r">Вектор</th><th className="r">Текст</th><th className="r">RRF</th><th className="r">Затухання</th><th className="r">Оцінка</th></tr>
                  </thead>
                  <tbody>
                    {trace.retrieved.map((r) => (
                      <tr key={r.id}>
                        <td><code>{r.id}</code></td>
                        <td>{r.title}</td>
                        <td className="nowrap num">{fmtTime(r.published_at)}</td>
                        <td className="r"><Rank v={r.rank_vec} /></td>
                        <td className="r"><Rank v={r.rank_fts} /></td>
                        <td className="r">{num(r.rrf, 4)}</td>
                        <td className="r">{num(r.decay, 3)}</td>
                        <td className="r">{r.score !== undefined ? num(r.score, 5) : r.offset_hours !== undefined ? `±${r.offset_hours} год` : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          )}

          {sub === 'verify' && trace.attempts.filter((a) => a.event === 'verify').map((a, i) => (
            <div className="attempt" key={i}>
              {a.ok ? <IconShield style={{ color: 'var(--good)' }} /> : <IconAlert style={{ color: 'var(--critical)' }} />}
              <div>
                <b>Спроба {a.attempt}</b> — {a.ok ? <span className="bar-pos">пройдено</span> : <span className="bar-neg">знайдено помилки</span>}
                {!!a.errors?.length && <ul>{a.errors.map((e, j) => <li key={j}><code>{e.check}</code> {e.message}</li>)}</ul>}
              </div>
            </div>
          ))}
        </>
      )}
    </section>
  )
}
