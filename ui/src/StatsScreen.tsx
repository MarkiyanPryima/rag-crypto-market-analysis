import { useEffect, useMemo, useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, type Stats } from './api'
import { fmtTime, num } from './format'

const TOPIC_UA: Record<string, string> = {
  regulation: 'регулювання',
  hacks_and_failures: 'злами й банкрутства',
  institutional: 'інституції',
  technology: 'технології',
  macro: 'макроекономіка',
  market: 'ринок',
  other: 'інше',
}

export function StatsScreen() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.stats().then(setStats).catch((e: Error) => setError(e.message))
  }, [])

  const months = useMemo(() => {
    if (!stats) return []
    const by: Record<string, Record<string, number>> = {}
    for (const r of stats.data.articles_by_month) {
      by[r.month] ??= {}
      const key = `${r.source} (${r.channel})`
      by[r.month][key] = (by[r.month][key] ?? 0) + r.n
    }
    return Object.entries(by).map(([month, v]) => ({ month, ...v }))
  }, [stats])
  const seriesKeys = useMemo(
    () => [...new Set(stats?.data.articles_by_month.map((r) => `${r.source} (${r.channel})`) ?? [])],
    [stats],
  )

  if (error) return <div className="card error-box" role="alert">{error}</div>
  if (!stats) return <div className="card muted">Завантаження статистики…</div>

  const t = stats.data.totals
  const palette = ['var(--c1)', 'var(--c2)', 'var(--c3)', 'var(--c4)']
  return (
    <div className="stats">
      <div className="metric-cards">
        {[
          ['Статей', t.articles],
          ['Фрагментів', t.chunks],
          ['З embedding', t.embedded],
          ['Розмічено', t.labelled],
          ['Епізодів', t.episodes],
          ['Запитів', t.queries],
        ].map(([label, v]) => (
          <div className="metric" key={label as string}>
            <div className="value">{num(v as number, 0)}</div>
            <div className="label">{label}</div>
          </div>
        ))}
      </div>

      <section className="card">
        <h3>Реакція ціни й волатильності на теми новин</h3>
        <p className="muted small">Зміна ціни за 6 год після публікації. Це збіг у часі, не причинний зв'язок.</p>
        {stats.topic_reaction.length === 0 ? (
          <p className="muted">Ще немає розмічених статей (потрібен ключ OpenAI для розмітки тем і тону).</p>
        ) : (
          <>
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={stats.topic_reaction.map((r) => ({ ...r, label: TOPIC_UA[r.topic] ?? r.topic }))}>
                <CartesianGrid stroke="var(--grid)" vertical={false} />
                <XAxis dataKey="label" stroke="var(--muted)" fontSize={11} />
                <YAxis stroke="var(--muted)" fontSize={11} unit="%" />
                <Tooltip contentStyle={{ background: 'var(--surface)', border: '1px solid var(--border)', fontSize: 12 }} />
                <Bar dataKey="avg_abs_move_6h_pct" name="Середній |рух| за 6 год, %" fill="var(--c1)" />
              </BarChart>
            </ResponsiveContainer>
            <table>
              <thead><tr><th>Тема</th><th>Статей</th><th>Сер. рух 6 год</th><th>Сер. |рух| 6 год</th><th>Волатильність після / до</th><th>Сер. тон</th></tr></thead>
              <tbody>
                {stats.topic_reaction.map((r) => (
                  <tr key={r.topic}>
                    <td>{TOPIC_UA[r.topic] ?? r.topic}</td><td>{r.articles}</td>
                    <td>{num(r.avg_move_6h_pct, 3)}%</td><td>{num(r.avg_abs_move_6h_pct, 3)}%</td>
                    <td>{num(r.vol_ratio_after_before, 2)}×</td><td>{num(r.avg_tone, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>

      <section className="card">
        <h3>Якість прогнозів</h3>
        {stats.forecasts.length === 0 ? (
          <p className="muted">Прогнози ще не згенеровано (моделі навчаються на завершальному етапі).</p>
        ) : (
          <table>
            <thead><tr><th>Актив</th><th>Тип</th><th>Прогнозів</th><th>Останній</th><th>Значення</th><th>Модель</th></tr></thead>
            <tbody>
              {stats.forecasts.map((f) => (
                <tr key={`${f.symbol}-${f.kind}`}>
                  <td>{f.symbol}</td><td>{f.kind}</td><td>{f.n}</td><td>{f.latest_date}</td>
                  <td>{num(f.latest_value, 4)}</td><td><code>{f.model_version}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h3>Обсяги даних: статті за місяцями</h3>
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={months}>
            <CartesianGrid stroke="var(--grid)" vertical={false} />
            <XAxis dataKey="month" stroke="var(--muted)" fontSize={11} />
            <YAxis stroke="var(--muted)" fontSize={11} />
            <Tooltip contentStyle={{ background: 'var(--surface)', border: '1px solid var(--border)', fontSize: 12 }} />
            {seriesKeys.map((k, i) => <Bar key={k} dataKey={k} stackId="a" fill={palette[i % palette.length]} />)}
          </BarChart>
        </ResponsiveContainer>
        <div className="legend">
          {seriesKeys.map((k, i) => <span key={k}><i className="sw" style={{ background: palette[i % palette.length] }} />{k}</span>)}
        </div>
        <table>
          <thead><tr><th>Актив</th><th>Свічок</th><th>З</th><th>По</th></tr></thead>
          <tbody>
            {stats.data.candles.map((c) => (
              <tr key={c.symbol}><td>{c.symbol}</td><td>{num(c.n, 0)}</td><td>{fmtTime(c.first)}</td><td>{fmtTime(c.last)}</td></tr>
            ))}
          </tbody>
        </table>
      </section>

      {stats.llm_usage.length > 0 && (
        <section className="card">
          <h3>Витрати на OpenAI</h3>
          <table>
            <thead><tr><th>Призначення</th><th>Модель</th><th>Викликів</th><th>Токени вх.</th><th>Токени вих.</th><th>$</th></tr></thead>
            <tbody>
              {stats.llm_usage.map((u) => (
                <tr key={`${u.purpose}-${u.model}`}>
                  <td>{u.purpose}</td><td><code>{u.model}</code></td><td>{u.calls}</td>
                  <td>{num(u.input_tokens, 0)}</td><td>{num(u.output_tokens, 0)}</td><td>{u.cost_usd === null ? '—' : num(u.cost_usd, 4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  )
}
