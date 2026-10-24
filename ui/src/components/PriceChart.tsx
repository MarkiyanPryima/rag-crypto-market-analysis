import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { Candle, NewsItem, Source } from '../api'
import { fmtTime } from '../format'

interface Props {
  candles: Candle[]
  news: NewsItem[]
  cited: Source[]
  asOf: string
}

const hour = (iso: string) => Math.floor(new Date(iso).getTime() / 3_600_000) * 3_600_000

/** Hourly close with markers for news (grey), cited sources (accent) and the query moment. */
export function PriceChart({ candles, news, cited, asOf }: Props) {
  if (!candles.length) return <p className="muted">Немає цін для цього вікна.</p>
  const data = candles.map((c) => ({ t: new Date(c.ts).getTime() + 3_600_000, close: c.close }))
  const citedIds = new Set(cited.map((c) => c.url))
  const first = data[0].t
  const last = new Date(asOf).getTime()
  const inRange = (t: number) => t >= first && t <= last
  return (
    <div className="chart">
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data} margin={{ top: 12, right: 12, bottom: 4, left: 4 }}>
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={[first, last]}
            tickFormatter={(t: number) => fmtTime(new Date(t).toISOString(), true)}
            stroke="var(--muted)"
            fontSize={11}
          />
          <YAxis domain={['auto', 'auto']} stroke="var(--muted)" fontSize={11} width={64}
                 tickFormatter={(v: number) => v.toLocaleString('en-US', { maximumFractionDigits: 0 })} />
          <Tooltip
            labelFormatter={(t) => `${fmtTime(new Date(Number(t)).toISOString())} UTC`}
            formatter={(v) => [Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 }), 'Ціна закриття']}
            contentStyle={{ background: 'var(--surface)', border: '1px solid var(--border)', fontSize: 12 }}
          />
          {news
            .filter((n) => !citedIds.has(n.url) && inRange(hour(n.published_at)))
            .map((n) => (
              <ReferenceLine key={n.id} x={hour(n.published_at)} stroke="var(--marker)" strokeDasharray="2 3" />
            ))}
          {cited
            .filter((s) => inRange(hour(s.published_at)))
            .map((s, i) => (
              <ReferenceLine key={s.id} x={hour(s.published_at)} stroke="var(--accent)"
                             label={{ value: `[${i + 1}]`, position: 'top', fill: 'var(--accent)', fontSize: 11 }} />
            ))}
          <ReferenceLine x={last} stroke="var(--text)" strokeWidth={1.5}
                         label={{ value: 'запит', position: 'insideTopRight', fill: 'var(--text)', fontSize: 11 }} />
          <Line type="monotone" dataKey="close" stroke="var(--line)" dot={false} strokeWidth={1.6} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      <div className="legend">
        <span><i className="sw line" />ціна</span>
        <span><i className="sw accent" />процитовані джерела</span>
        <span><i className="sw marker" />інші новини</span>
        <span><i className="sw text" />момент запиту</span>
      </div>
    </div>
  )
}
