// Dates are always shown in UTC: the whole system reasons in UTC.

export function fmtTime(iso: string, short = false): string {
  const d = new Date(iso)
  const p = (n: number) => String(n).padStart(2, '0')
  const date = `${p(d.getUTCDate())}.${p(d.getUTCMonth() + 1)}`
  const time = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`
  return short ? `${date} ${time}` : `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${time}`
}

/** <input type="datetime-local"> value (treated as UTC) <-> ISO string. */
export const localInputToIso = (v: string) => (v ? `${v}:00Z` : undefined)
export const isoToLocalInput = (iso: string) => iso.slice(0, 16)

export const nowInput = () => isoToLocalInput(new Date().toISOString())

export const num = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? '—' : v.toLocaleString('en-US', { maximumFractionDigits: digits })

/** "3 год до запиту" / "2 дні до запиту" relative to a reference moment (as_of). */
export function relativeTo(iso: string, refIso: string): string {
  const h = (new Date(refIso).getTime() - new Date(iso).getTime()) / 3_600_000
  const abs = Math.abs(h)
  const when = h >= 0 ? 'до запиту' : 'після запиту'
  if (abs < 1) return `${Math.max(1, Math.round(abs * 60))} хв ${when}`
  if (abs < 48) return `${Math.round(abs)} год ${when}`
  return `${Math.round(abs / 24)} дн ${when}`
}
