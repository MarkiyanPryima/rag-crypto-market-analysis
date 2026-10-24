import { useState } from 'react'
import { api, type Trace } from '../api'
import { fmtTime, num } from '../format'

/** "Як отримано відповідь": routing, tool calls, retrieved chunks with ranks, verification. */
export function TraceView({ queryId }: { queryId: number }) {
  const [trace, setTrace] = useState<Trace | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)

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
    <section className="card trace">
      <button className="link" onClick={toggle} aria-expanded={open}>
        {open ? '▾' : '▸'} Як отримано відповідь (запит #{queryId})
      </button>
      {open && error && <p className="error">{error}</p>}
      {open && !trace && !error && <p className="muted">Завантаження…</p>}
      {open && trace && (
        <div className="trace-body">
          <h4>Маршрутизація</h4>
          <p>
            Намір: <b>{trace.intent}</b> · актив: <b>{trace.routing?.asset_used ?? trace.asset}</b> · вікно:{' '}
            <b>{trace.routing?.window_hours ?? '—'} год</b> · as_of: <b>{fmtTime(trace.as_of)} UTC</b>
          </p>
          {typeof trace.routing?.output?.reason === 'string' && <p className="muted">{trace.routing.output.reason}</p>}

          <h4>Виклики інструментів</h4>
          <table>
            <thead><tr><th>Інструмент</th><th>Аргументи</th><th>мс</th><th>Примітка</th></tr></thead>
            <tbody>
              {trace.tool_calls.map((c, i) => (
                <tr key={i}>
                  <td><code>{c.name}</code></td>
                  <td><code>{JSON.stringify(c.args)}</code></td>
                  <td>{c.ms ?? '—'}</td>
                  <td>{c.error ? <span className="error">{String(c.error)}</span>
                    : c.ignored_model_as_of ? `дату моделі (${String(c.ignored_model_as_of)}) проігноровано` : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h4>Знайдені фрагменти</h4>
          <table>
            <thead>
              <tr><th>id</th><th>Заголовок</th><th>Дата</th><th>ранг vec</th><th>ранг fts</th><th>RRF</th><th>затухання</th><th>оцінка</th></tr>
            </thead>
            <tbody>
              {trace.retrieved.map((r) => (
                <tr key={r.id}>
                  <td><code>{r.id}</code></td>
                  <td>{r.title}</td>
                  <td className="nowrap">{fmtTime(r.published_at)}</td>
                  <td>{r.rank_vec ?? '—'}</td>
                  <td>{r.rank_fts ?? '—'}</td>
                  <td>{num(r.rrf, 4)}</td>
                  <td>{num(r.decay, 3)}</td>
                  <td>{r.score !== undefined ? num(r.score, 5) : r.offset_hours !== undefined ? `±${r.offset_hours} год` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h4>Перевірка</h4>
          <ol>
            {trace.attempts.filter((a) => a.event === 'verify').map((a, i) => (
              <li key={i}>
                Спроба {a.attempt}: {a.ok ? <span className="ok">пройдено</span> : <span className="error">помилки</span>}
                {!!a.errors?.length && (
                  <ul>{a.errors.map((e, j) => <li key={j}><code>{e.check}</code> {e.message}</li>)}</ul>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  )
}
