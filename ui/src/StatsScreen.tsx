import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, type Stats } from './api'
import { fmtTime, num } from './format'
import { IconAlert, IconCoins, IconDatabase, IconHistory, IconInfo, IconNews, IconPulse, IconTarget } from './icons'

const TOPIC_UA: Record<string, string> = {
  regulation: 'Регулювання',
  hacks_and_failures: 'Злами й банкрутства',
  institutional: 'Інституції',
  technology: 'Технології',
  macro: 'Макроекономіка',
  market: 'Ринок',
  other: 'Інше',
}
// Validated categorical slots 1-3; series keep their slot whatever the filter.
const SERIES = ['var(--s1)', 'var(--s2)', 'var(--s3)']
const SOURCE_LABEL: Record<string, string> = {
  'guardian (api)': 'The Guardian',
  'coindesk (rss)': 'CoinDesk RSS',
  'coindesk (gdelt)': 'CoinDesk через GDELT',
}

function Tip({ active, payload, label, unit = '' }: { active?: boolean; payload?: Array<{ name: string; value: number; color?: string }>; label?: string; unit?: string }) {
  if (!active || !payload?.length) return null
  return (
    <div className="tip">
      <div className="t-head">{label}</div>
      {payload.map((p) => (
        <div key={p.name} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {payload.length > 1 && <i className="sw" style={{ background: p.color }} />}
          <span>{p.name}</span><b className="num" style={{ marginLeft: 'auto' }}>{num(p.value, 3)}{unit}</b>
        </div>
      ))}
    </div>
  )
}

function Stat({ icon, value, label }: { icon: ReactNode; value: string; label: string }) {
  return (
    <div className="stat">
      <span className="ico">{icon}</span>
      <div><div className="value">{value}</div><div className="label">{label}</div></div>
    </div>
  )
}

export function StatsScreen() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.stats().then(setStats).catch((e: Error) => setError(e.message))
  }, [])

  const keys = useMemo(
    () => [...new Set(stats?.data.articles_by_month.map((r) => `${r.source} (${r.channel})`) ?? [])].sort(),
    [stats],
  )
  const months = useMemo(() => {
    if (!stats) return []
    const by: Record<string, Record<string, number>> = {}
    for (const r of stats.data.articles_by_month) {
      by[r.month] ??= {}
      by[r.month][`${r.source} (${r.channel})`] = (by[r.month][`${r.source} (${r.channel})`] ?? 0) + r.n
    }
    return Object.entries(by).map(([month, v]) => ({ month, ...Object.fromEntries(keys.map((k) => [k, v[k] ?? 0])) }))
  }, [stats, keys])
  const totalsBySource = useMemo(
    () => keys.map((k) => ({ k, n: months.reduce((s, m) => s + ((m as unknown as Record<string, number>)[k] ?? 0), 0) })),
    [keys, months],
  )

  if (error) return <div className="error-box" role="alert"><IconAlert />{error}</div>
  if (!stats) return <div className="card"><div className="skeleton" /><div className="skeleton" style={{ width: '60%', marginTop: 10 }} /></div>

  const t = stats.data.totals
  const spend = stats.llm_usage.reduce((s, u) => s + Number(u.cost_usd ?? 0), 0)
  const topics = stats.topic_reaction.map((r) => ({
    ...r,
    avg_move_6h_pct: Number(r.avg_move_6h_pct),
    avg_abs_move_6h_pct: Number(r.avg_abs_move_6h_pct),
    vol_ratio_after_before: r.vol_ratio_after_before === null ? null : Number(r.vol_ratio_after_before),
    avg_tone: Number(r.avg_tone),
    label: TOPIC_UA[r.topic] ?? r.topic,
  }))

  return (
    <div className="stack">
      <div className="stat-grid">
        <Stat icon={<IconNews />} value={num(t.articles, 0)} label="Статей" />
        <Stat icon={<IconDatabase />} value={num(t.embedded, 0)} label={`Фрагментів з embedding (з ${num(t.chunks, 0)})`} />
        <Stat icon={<IconTarget />} value={num(t.labelled, 0)} label="Розмічено тему й тон" />
        <Stat icon={<IconHistory />} value={num(t.episodes, 0)} label="48-годинних епізодів" />
        <Stat icon={<IconPulse />} value={num(t.queries, 0)} label="Запитів до системи" />
        <Stat icon={<IconCoins />} value={`$${spend.toFixed(2)}`} label="Витрати на OpenAI" />
      </div>

      <div className="two-col">
        <section className="card">
          <h3 className="card-title"><span className="ico orange"><IconPulse /></span>Реакція ціни на теми новин</h3>
          <p className="card-sub">Середній абсолютний рух ціни за 6 год після публікації. Це збіг у часі, не причинний зв'язок.</p>
          {topics.length === 0 ? (
            <div className="empty"><IconInfo />Ще немає розмічених статей.</div>
          ) : (
            <>
              <ResponsiveContainer width="100%" height={240}>
                <BarChart data={topics} layout="vertical" margin={{ left: 8, right: 24 }}>
                  <CartesianGrid stroke="var(--grid)" horizontal={false} />
                  <XAxis type="number" unit="%" stroke="var(--muted)" fontSize={11} tickLine={false} axisLine={false} />
                  <YAxis type="category" dataKey="label" width={138} stroke="var(--muted)" fontSize={12} tickLine={false} axisLine={false} />
                  <Tooltip content={<Tip unit="%" />} cursor={{ fill: 'var(--surface-2)' }} />
                  <Bar dataKey="avg_abs_move_6h_pct" name="|рух| за 6 год" fill="var(--s1)" radius={[0, 4, 4, 0]} barSize={16} />
                </BarChart>
              </ResponsiveContainer>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Тема</th><th className="r">Статей</th><th className="r">Рух 6 год</th><th className="r">|Рух|</th><th className="r">Волат. після/до</th><th className="r">Тон</th></tr></thead>
                  <tbody>
                    {topics.map((r) => (
                      <tr key={r.topic}>
                        <td>{r.label}</td>
                        <td className="r">{r.articles}</td>
                        <td className={`r ${r.avg_move_6h_pct >= 0 ? 'bar-pos' : 'bar-neg'}`}>{r.avg_move_6h_pct >= 0 ? '+' : ''}{num(r.avg_move_6h_pct, 3)}%</td>
                        <td className="r">{num(r.avg_abs_move_6h_pct, 3)}%</td>
                        <td className="r">{num(r.vol_ratio_after_before, 2)}×</td>
                        <td className="r">{num(r.avg_tone, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </section>

        <section className="card">
          <h3 className="card-title"><span className="ico aqua"><IconNews /></span>Статті за місяцями</h3>
          <p className="card-sub">Кількість зібраних статей за джерелом і каналом.</p>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={months} margin={{ right: 8 }}>
              <CartesianGrid stroke="var(--grid)" vertical={false} />
              <XAxis dataKey="month" stroke="var(--muted)" fontSize={11} tickLine={false} axisLine={false} minTickGap={16} />
              <YAxis stroke="var(--muted)" fontSize={11} tickLine={false} axisLine={false} width={36} />
              <Tooltip content={<Tip />} cursor={{ fill: 'var(--surface-2)' }} />
              {keys.map((k, i) => (
                <Bar key={k} dataKey={k} name={SOURCE_LABEL[k] ?? k} stackId="a" fill={SERIES[i % SERIES.length]}
                     stroke="var(--surface)" strokeWidth={2} radius={i === keys.length - 1 ? [4, 4, 0, 0] : 0}>
                  {months.map((_, j) => <Cell key={j} />)}
                </Bar>
              ))}
            </BarChart>
          </ResponsiveContainer>
          <div className="legend">
            {totalsBySource.map(({ k, n }, i) => (
              <span key={k}><i className="sw" style={{ background: SERIES[i % SERIES.length] }} />{SOURCE_LABEL[k] ?? k} · <b className="num">{n}</b></span>
            ))}
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Актив</th><th className="r">Годинних свічок</th><th>З</th><th>По</th></tr></thead>
              <tbody>
                {stats.data.candles.map((c) => (
                  <tr key={c.symbol}><td>{c.symbol}</td><td className="r">{num(c.n, 0)}</td><td className="num">{fmtTime(c.first)}</td><td className="num">{fmtTime(c.last)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section className="card">
        <h3 className="card-title"><span className="ico violet"><IconTarget /></span>Прогнози</h3>
        {stats.forecasts.length === 0 ? (
          <div className="empty"><IconInfo />Прогнози ще не згенеровано: моделі навчаються на завершальному етапі.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Актив</th><th>Тип</th><th className="r">Прогнозів</th><th>Останній</th><th className="r">Значення</th><th>Модель</th></tr></thead>
              <tbody>
                {stats.forecasts.map((f) => (
                  <tr key={`${f.symbol}-${f.kind}`}>
                    <td>{f.symbol}</td><td>{f.kind}</td><td className="r">{f.n}</td><td>{f.latest_date}</td>
                    <td className="r">{num(f.latest_value, 4)}</td><td><code>{f.model_version}</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {stats.llm_usage.length > 0 && (
        <section className="card">
          <h3 className="card-title"><span className="ico"><IconCoins /></span>Витрати на OpenAI</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Призначення</th><th>Модель</th><th className="r">Викликів</th><th className="r">Токени вх.</th><th className="r">Токени вих.</th><th className="r">$</th></tr></thead>
              <tbody>
                {stats.llm_usage.map((u) => (
                  <tr key={`${u.purpose}-${u.model}`}>
                    <td>{u.purpose}</td><td><code>{u.model}</code></td><td className="r">{u.calls}</td>
                    <td className="r">{num(u.input_tokens, 0)}</td><td className="r">{num(u.output_tokens, 0)}</td>
                    <td className="r">{u.cost_usd === null ? '—' : num(u.cost_usd, 4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  )
}
