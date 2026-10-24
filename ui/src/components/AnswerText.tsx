import { Fragment, type ReactNode } from 'react'
import type { Metric, Source } from '../api'

interface Props {
  text: string
  sources: Source[]
  metrics: Metric[]
}

const REF = /\[(c_\d+|m\d+|f\d+)\]|\*\*(.+?)\*\*/g

/** Inline markup: [c_n] -> numbered source link, [m_n]/[f_n] -> metric chip, **bold**. */
function inline(line: string, sources: Source[], metrics: Metric[], key: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  for (const m of line.matchAll(REF)) {
    const at = m.index ?? 0
    if (at > last) out.push(line.slice(last, at))
    if (m[2] !== undefined) {
      out.push(<strong key={`${key}-${at}`}>{m[2]}</strong>)
    } else if (m[1].startsWith('c_')) {
      const n = sources.findIndex((s) => s.id === m[1])
      const src = sources[n]
      out.push(
        src ? (
          <a key={`${key}-${at}`} className="cite" href={`#src-${src.id}`} title={src.title}>
            [{n + 1}]
          </a>
        ) : (
          <span key={`${key}-${at}`} className="cite missing">[?]</span>
        ),
      )
    } else {
      const met = metrics.find((x) => x.id === m[1])
      out.push(
        <span key={`${key}-${at}`} className="ref" title={met?.description ?? m[1]}>
          {m[1]}
        </span>,
      )
    }
    last = at + m[0].length
  }
  if (last < line.length) out.push(line.slice(last))
  return out
}

/** Paragraphs and "- " bullet lists; headings are handled by the caller. */
export function AnswerText({ text, sources, metrics }: Props) {
  const blocks = text.split(/\n\s*\n/)
  return (
    <>
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
    </>
  )
}
