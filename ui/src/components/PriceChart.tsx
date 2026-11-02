import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { Candle, NewsItem, Source } from '../api'
import { fmtTime } from '../format'

interface Props {
  candles: Candle[]
  news: NewsItem[]
  cited: Source[]
  asOf: string
}

const HOUR = 3_600_000
const hour = (iso: string) => Math.floor(new Date(iso).getTime() / HOUR) * HOUR

interface TipProps {
  active?: boolean
  label?: number
  payload?: Array<{ value: number }>
  newsByHour: Map<number, string[]>
}

function ChartTip({ active, label, payload, newsByHour }: TipProps) {
  if (!active || !payload?.length || label === undefined) return null
  const items = newsByHour.get(Math.floor((label - HOUR) / HOUR) * HOUR) ?? []
  return (
    <div className="tip">
      <div className="t-head">{fmtTime(new Date(label).toISOString())} UTC</div>
      <div className="t-val">{payload[0].value.toLocaleString('en-US', { maximumFractionDigits: 2 })} USDT</div>
      {items.length > 0 && (
        <div className="t-news">
          {items.slice(0, 3).map((t, i) => <div key={i}>• {t}</div>)}
          {items.length > 3 && <div className="muted">+{items.length - 3} ще</div>}
        </div>
      )}
    </div>
  )
}

/** Hourly close as an area, with markers for cited sources, other news and the query moment. */
export function PriceChart({ candles, news, cited, asOf }: Props) {
  if (!candles.length) return <div className="empty">Немає цін для цього вікна.</div>
  // A candle's close is known at ts + 1 h, so the point sits at its close time.
  const data = candles.map((c) => ({ t: new Date(c.ts).getTime() + HOUR, close: c.close }))
  const first = data[0].t
  const last = new Date(asOf).getTime()
  const inRange = (t: number) => t >= first && t <= last
  const citedUrls = new Set(cited.map((c) => c.url))
  const newsByHour = new Map<number, string[]>()
  for (const n of news) {
    const h = hour(n.published_at)
    newsByHour.set(h, [...(newsByHour.get(h) ?? []), n.title])
  }
  // Round axis ticks: 1/2/5 x 10^n steps around the price range.
  const lo = Math.min(...data.map((d) => d.close))
  const hi = Math.max(...data.map((d) => d.close))
  const raw = (hi - lo || hi * 0.01) / 4
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 5, 10].map((f) => f * mag).find((v) => v >= raw) ?? 10 * mag
  const yMin = Math.floor(lo / step) * step
  const yMax = Math.ceil(hi / step) * step
  const ticks = Array.from({ length: Math.round((yMax - yMin) / step) + 1 }, (_, i) => yMin + i * step)
  return (
    <div className="chart-wrap">
      <ResponsiveContainer width="100%" height={280}>
        <AreaChart data={data} margin={{ top: 18, right: 12, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="priceFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--s1)" stopOpacity={0.28} />
              <stop offset="100%" stopColor="var(--s1)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={[first, last]} tickLine={false} axisLine={false}
                 tickFormatter={(t: number) => fmtTime(new Date(t).toISOString(), true)} stroke="var(--muted)" fontSize={11} minTickGap={40} />
          <YAxis domain={[yMin, yMax]} ticks={ticks} tickLine={false} axisLine={false} stroke="var(--muted)" fontSize={11} width={62}
                 tickFormatter={(v: number) => v.toLocaleString('en-US', { maximumFractionDigits: 0 })} />
          <Tooltip content={<ChartTip newsByHour={newsByHour} />} cursor={{ stroke: 'var(--border-strong)', strokeWidth: 1 }} />
          {news
            .filter((n) => !citedUrls.has(n.url) && inRange(hour(n.published_at)))
            .map((n) => <ReferenceLine key={n.id} x={hour(n.published_at)} stroke="var(--marker)" strokeDasharray="3 4" />)}
          {cited
            .filter((s) => inRange(hour(s.published_at)))
            .map((s) => (
              <ReferenceLine key={s.id} x={hour(s.published_at)} stroke="var(--s2)" strokeWidth={2}
                             label={{ value: String(cited.indexOf(s) + 1), position: 'top', fill: 'var(--s2)', fontSize: 11, fontWeight: 700 }} />
            ))}
          <ReferenceLine x={last} stroke="var(--text)" strokeWidth={1.5}
                         label={{ value: 'запит', position: 'insideTopRight', fill: 'var(--text-2)', fontSize: 11 }} />
          <Area type="monotone" dataKey="close" stroke="var(--s1)" strokeWidth={2} fill="url(#priceFill)" dot={false}
                activeDot={{ r: 4, stroke: 'var(--surface)', strokeWidth: 2 }} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
      <div className="legend">
        <span><i className="sw" style={{ background: 'var(--s1)' }} />Ціна закриття</span>
        <span><i className="sw" style={{ background: 'var(--s2)' }} />Процитовані джерела</span>
        <span><i className="sw dash" />Інші новини</span>
        <span><i className="sw" style={{ background: 'var(--text)' }} />Момент запиту</span>
      </div>
    </div>
  )
}
