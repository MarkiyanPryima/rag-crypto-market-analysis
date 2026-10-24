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
