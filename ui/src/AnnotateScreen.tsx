import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type AnnotationItem,
  type AnnotationTask,
  type ClaimsPayload,
  type LabelsPayload,
  type RelevancePayload,
} from './api'
import { fmtTime } from './format'

// Manual annotation for the evaluation (stage 17). Model outputs (judge verdicts,
// model labels) are never sent by the API, so the author judges blind.

const TASKS: Array<{ key: AnnotationTask; title: string; hint: string }> = [
  {
    key: 'relevance',
    title: 'Релевантність статей',
    hint: 'Чи стосується стаття руху ціни, про який запитання? Відповідайте за заголовком і фрагментом; час публікації показано відносно вікна руху.',
  },
  {
    key: 'claims',
    title: 'Перевірка тверджень',
    hint: 'Для кожного твердження: чи підтверджує його контекст (результати інструментів і новини), який бачила модель? Власні знання не враховуються.',
  },
  {
    key: 'labels',
    title: 'Тема й тон',
    hint: 'Оберіть тему і тон статті щодо ринку криптовалют.',
  },
]

const TOPICS: Record<string, string> = {
  regulation: 'Регулювання',
  hacks_and_failures: 'Зломи й банкрутства',
  institutional: 'Інституційні гравці',
  technology: 'Технології',
  macro: 'Макроекономіка',
  market: 'Ринок',
  other: 'Інше',
}
const TONES: Record<string, string> = { negative: 'Негативний', neutral: 'Нейтральний', positive: 'Позитивний' }

type Progress = Record<string, { total: number; done: number }>

export function AnnotateScreen() {
  const [task, setTask] = useState<AnnotationTask>('relevance')
  const [progress, setProgress] = useState<Progress>({})
  const [error, setError] = useState<string | null>(null)

  const refresh = () => api.annotationProgress().then(setProgress).catch((e: Error) => setError(e.message))
  useEffect(() => {
    refresh()
  }, [])

  const current = TASKS.find((t) => t.key === task)!
  return (
    <div className="stack">
      <section className="card">
        <h3 className="card-title">Ручна розмітка для оцінювання</h3>
        <p className="card-sub" style={{ marginLeft: 0 }}>
          Ваші позначки — еталон для повноти пошуку, перевірки судді та якості міток. Мітки моделей тут не показано.
        </p>
        <div className="segmented" role="tablist">
          {TASKS.map((t) => (
            <button key={t.key} role="tab" className={task === t.key ? 'on' : ''} onClick={() => setTask(t.key)}>
              {t.title}{' '}
              <span className="muted num">
                {progress[t.key] ? `${progress[t.key].done}/${progress[t.key].total}` : '—'}
              </span>
            </button>
          ))}
        </div>
        <p className="small muted" style={{ marginBottom: 0 }}>{current.hint}</p>
      </section>
      {error && <div className="card badge bad">{error}</div>}
      {task === 'relevance' && <Relevance onSaved={refresh} onError={setError} />}
      {task === 'claims' && <Claims onSaved={refresh} onError={setError} />}
      {task === 'labels' && <Labels onSaved={refresh} onError={setError} />}
    </div>
  )
}

interface TaskProps {
  onSaved: () => void
  onError: (message: string) => void
}

function useItems<P, L>(task: AnnotationTask, onError: (m: string) => void) {
  const [items, setItems] = useState<AnnotationItem<P, L>[] | null>(null)
  useEffect(() => {
    api.annotations<P, L>(task).then(setItems).catch((e: Error) => onError(e.message))
  }, [task, onError])
  const save = (item: AnnotationItem<P, L>, label: L, onSaved: () => void) =>
    api
      .saveAnnotation(task, item.item_id, label as Record<string, unknown>)
      .then(() => {
        setItems((prev) => prev?.map((x) => (x.item_id === item.item_id ? { ...x, label } : x)) ?? null)
        onSaved()
      })
      .catch((e: Error) => onError(e.message))
  return { items, save }
}

function hoursFrom(iso: string, refIso: string): string {
  const h = (new Date(iso).getTime() - new Date(refIso).getTime()) / 3_600_000
  return `${h >= 0 ? '+' : '−'}${Math.abs(h).toFixed(1)} год`
}

function Relevance({ onSaved, onError }: TaskProps) {
  const { items, save } = useItems<RelevancePayload, { relevant: boolean }>('relevance', onError)
  const groups = useMemo(() => {
    const out = new Map<string, AnnotationItem<RelevancePayload, { relevant: boolean }>[]>()
    for (const it of items ?? []) out.set(it.payload.question_id, [...(out.get(it.payload.question_id) ?? []), it])
    return [...out.values()]
  }, [items])
  if (!items) return <div className="card"><div className="skeleton" /></div>
  if (!items.length) return <Empty />
  return (
    <>
      {groups.map((g) => {
        const q = g[0].payload
        const done = g.filter((x) => x.label).length
        return (
          <section className="card" key={q.question_id}>
            <h3 className="card-title">
              {q.question}
              <span className={`badge ${q.move_pct < 0 ? 'bad' : 'ok'}`}>{q.move_pct > 0 ? '+' : ''}{q.move_pct}%</span>
              <span className="muted small num">{done}/{g.length}</span>
            </h3>
            <p className="card-sub" style={{ marginLeft: 0 }}>
              Вікно руху: {fmtTime(q.window_start)} – {fmtTime(q.window_end)} UTC
            </p>
            {g.map((it) => {
              const a = it.payload.article
              return (
                <div key={it.item_id} className="annot-row">
                  <div className="annot-text">
                    <a href={a.url} target="_blank" rel="noreferrer">{a.title}</a>
                    <div className="small muted">
                      {a.source} · {fmtTime(a.published_at)} UTC ({hoursFrom(a.published_at, q.window_start)} від початку вікна)
                    </div>
                    {(a.standfirst || a.body) && <div className="small annot-snippet">{a.standfirst || a.body}</div>}
                  </div>
                  <div className="segmented">
                    <button className={it.label?.relevant === true ? 'on' : ''} onClick={() => save(it, { relevant: true }, onSaved)}>Так</button>
                    <button className={it.label?.relevant === false ? 'on' : ''} onClick={() => save(it, { relevant: false }, onSaved)}>Ні</button>
                  </div>
                </div>
              )
            })}
          </section>
        )
      })}
    </>
  )
}

function Claims({ onSaved, onError }: TaskProps) {
  const { items, save } = useItems<ClaimsPayload, { supported: boolean[] }>('claims', onError)
  const [index, setIndex] = useState(0)
  const [marks, setMarks] = useState<Array<boolean | null>>([])
  const item = items?.[index]
  useEffect(() => {
    if (item) setMarks(item.label?.supported ?? item.payload.claims.map(() => null))
  }, [item])
  if (!items) return <div className="card"><div className="skeleton" /></div>
  if (!item) return <Empty />
  const p = item.payload
  const complete = marks.length > 0 && marks.every((m) => m !== null)
  return (
    <section className="card">
      <div className="annot-pager">
        <button className="chip" disabled={index === 0} onClick={() => setIndex(index - 1)}>← Назад</button>
        <span className="num">{index + 1} / {items.length} {item.label ? '· збережено' : ''}</span>
        <button className="chip" disabled={index === items.length - 1} onClick={() => setIndex(index + 1)}>Далі →</button>
      </div>
      <h3 className="card-title">{p.question}</h3>
      <p className="small muted">станом на {fmtTime(p.as_of)} UTC</p>
      <div className="annot-cols">
        <div>
          <h4>Відповідь</h4>
          <div className="annot-pre">{p.answer}</div>
        </div>
        <div>
          <h4>Контекст, який бачила модель</h4>
          <div className="annot-pre annot-context">{p.context}</div>
        </div>
      </div>
      <h4>Твердження</h4>
      {p.claims.map((c, i) => (
        <div key={i} className="annot-row">
          <div className="annot-text">{c}</div>
          <div className="segmented">
            <button className={marks[i] === true ? 'on' : ''} onClick={() => setMarks(marks.map((m, j) => (j === i ? true : m)))}>Підтверджено</button>
            <button className={marks[i] === false ? 'on' : ''} onClick={() => setMarks(marks.map((m, j) => (j === i ? false : m)))}>Ні</button>
          </div>
        </div>
      ))}
      <button
        className="btn-primary"
        style={{ marginTop: 14 }}
        disabled={!complete}
        onClick={() =>
          save(item, { supported: marks as boolean[] }, () => {
            onSaved()
            if (index < items.length - 1) setIndex(index + 1)
          })
        }
      >
        Зберегти й далі
      </button>
    </section>
  )
}

function Labels({ onSaved, onError }: TaskProps) {
  const { items, save } = useItems<LabelsPayload, { topic: string; tone: string }>('labels', onError)
  const [draft, setDraft] = useState<Record<string, { topic?: string; tone?: string }>>({})
  if (!items) return <div className="card"><div className="skeleton" /></div>
  if (!items.length) return <Empty />
  const pick = (id: string, patch: { topic?: string; tone?: string }, saved: { topic: string; tone: string } | null) => {
    const next = { ...saved, ...draft[id], ...patch }
    setDraft({ ...draft, [id]: next })
    return next
  }
  return (
    <>
      {items.map((it) => {
        const v = { ...it.label, ...draft[it.item_id] }
        const choose = (patch: { topic?: string; tone?: string }) => {
          const next = pick(it.item_id, patch, it.label)
          if (next.topic && next.tone) save(it, { topic: next.topic, tone: next.tone }, onSaved)
        }
        return (
          <section className="card" key={it.item_id}>
            <h3 className="card-title"><a href={it.payload.url} target="_blank" rel="noreferrer">{it.payload.title}</a></h3>
            <p className="small muted">{it.payload.source} · {fmtTime(it.payload.published_at)} UTC {it.label ? '· збережено' : ''}</p>
            {it.payload.standfirst && <p className="small">{it.payload.standfirst}</p>}
            {it.payload.body && <p className="small annot-snippet">{it.payload.body}</p>}
            <div className="annot-choices">
              {Object.entries(TOPICS).map(([k, t]) => (
                <button key={k} className={`chip ${v.topic === k ? 'on' : ''}`} onClick={() => choose({ topic: k })}>{t}</button>
              ))}
            </div>
            <div className="annot-choices">
              {Object.entries(TONES).map(([k, t]) => (
                <button key={k} className={`chip ${v.tone === k ? 'on' : ''}`} onClick={() => choose({ tone: k })}>{t}</button>
              ))}
            </div>
          </section>
        )
      })}
    </>
  )
}

function Empty() {
  return (
    <div className="card muted">
      Завдань ще немає. Їх створює команда <code>python -m crypto_rag.evaluation.annotate build</code> після прогонів оцінювання.
    </div>
  )
}
