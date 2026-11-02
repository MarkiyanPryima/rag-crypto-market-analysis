import { Fragment, type ReactNode } from 'react'
import type { Metric, Source } from '../api'
import { fmtTime } from '../format'

interface Props {
  text: string
  sources: Source[]
  metrics: Metric[]
}

const REF = /\[(c_\d+|m\d+|f\d+)\]|\*\*(.+?)\*\*/g

/** Inline markup: [c_n] -> numbered source chip, [m_n]/[f_n] -> metric reference, **bold**. */
function inline(line: string, sources: Source[], metrics: Metric[], key: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  for (const m of line.matchAll(REF)) {
    const at = m.index ?? 0
    if (at > last) out.push(line.slice(last, at))
    const k = `${key}-${at}`
    if (m[2] !== undefined) {
      out.push(<strong key={k}>{m[2]}</strong>)
    } else if (m[1].startsWith('c_')) {
      const n = sources.findIndex((s) => s.id === m[1])
      const src = sources[n]
      out.push(
        src ? (
          <a key={k} className="cite" href={`#src-${src.id}`} title={`${src.title} · ${fmtTime(src.published_at)} UTC`}>
            {n + 1}
          </a>
        ) : (
          <span key={k} className="cite missing" title="Джерело не знайдено">?</span>
        ),
      )
    } else {
      const met = metrics.find((x) => x.id === m[1])
      out.push(
        <span key={k} className="ref" title={met ? `${met.display} — ${met.description}` : m[1]}>
          {m[1]}
        </span>,
      )
    }
    last = at + m[0].length
  }
  if (last < line.length) out.push(line.slice(last))
  return out
}

/** Paragraphs and "- " bullet lists; section headings are rendered by the caller. */
export function AnswerText({ text, sources, metrics }: Props) {
  const blocks = text.replace(/^\s*#{1,6}\s.*$/gm, '').split(/\n\s*\n/).filter((b) => b.trim())
  return (
    <div className="prose">
      {blocks.map((block, i) => {
        const lines = block.split('\n').filter((l) => l.trim())
        if (lines.length && lines.every((l) => /^\s*[-*•]\s+/.test(l))) {
          return (
            <ul key={i}>
              {lines.map((l, j) => (
                <li key={j}>{inline(l.replace(/^\s*[-*•]\s+/, ''), sources, metrics, `${i}-${j}`)}</li>
              ))}
            </ul>
          )
        }
        return (
          <p key={i}>
            {lines.map((l, j) => (
              <Fragment key={j}>
                {j > 0 && <br />}
                {inline(l, sources, metrics, `${i}-${j}`)}
              </Fragment>
            ))}
          </p>
        )
      })}
    </div>
  )
}
