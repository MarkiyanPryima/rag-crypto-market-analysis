import { useEffect, useState } from 'react'
import { api, type Health } from './api'
import { ErrorBoundary } from './ErrorBoundary'
import { fmtTime } from './format'
import { IconLogo, IconMoon, IconSun } from './icons'
import { QueryScreen } from './QueryScreen'
import { StatsScreen } from './StatsScreen'

type Tab = 'query' | 'stats'
type Theme = 'light' | 'dark'

function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem('theme')
    if (saved === 'light' || saved === 'dark') return saved
  } catch {
    /* storage unavailable */
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export default function App() {
  const [tab, setTab] = useState<Tab>('query')
  const [theme, setTheme] = useState<Theme>(initialTheme)
  const [health, setHealth] = useState<Health | null>(null)
  const [healthError, setHealthError] = useState(false)
  // Data is "live" if the newest closed candle is at most ~3 h old (checked on each poll).
  const [fresh, setFresh] = useState(false)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem('theme', theme)
    } catch {
      /* storage unavailable */
    }
  }, [theme])

  useEffect(() => {
    const load = () =>
      api.health()
        .then((h) => {
          setHealth(h)
          setHealthError(false)
          setFresh(!!h.last_candle && Date.now() - new Date(h.last_candle).getTime() < 3 * 3_600_000)
        })
        .catch(() => setHealthError(true))
    load()
    const id = setInterval(load, 60_000)
    return () => clearInterval(id)
  }, [])

  return (
    <>
      <header className="top">
        <div className="top-inner">
          <div className="brand">
            <IconLogo width={34} height={34} />
            <div className="brand-text">
              <strong>Crypto RAG</strong>
              <span>Аналіз ринку BTC/ETH: новини + ринкові дані</span>
            </div>
          </div>
          <nav className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === 'query'} className={tab === 'query' ? 'on' : ''} onClick={() => setTab('query')}>
              Запит
            </button>
            <button role="tab" aria-selected={tab === 'stats'} className={tab === 'stats' ? 'on' : ''} onClick={() => setTab('stats')}>
              Статистика
            </button>
          </nav>
          <div className="top-actions">
            <span className="status-pill" title={health?.last_candle ? `Остання свічка: ${fmtTime(health.last_candle)} UTC` : ''}>
              <i className={`dot ${healthError ? 'bad' : fresh ? 'good' : ''}`} />
              <span className="status-text">
                {healthError ? 'API недоступний' : health ? (fresh ? 'Дані актуальні' : 'Дані відстають') : 'Перевірка…'}
              </span>
            </span>
            <button className="icon-btn" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
                    aria-label={theme === 'dark' ? 'Світла тема' : 'Темна тема'} title={theme === 'dark' ? 'Світла тема' : 'Темна тема'}>
              {theme === 'dark' ? <IconSun /> : <IconMoon />}
            </button>
          </div>
        </div>
      </header>
      <div className="app">
        <main>
          <ErrorBoundary key={tab}>{tab === 'query' ? <QueryScreen /> : <StatsScreen />}</ErrorBoundary>
        </main>
      </div>
    </>
  )
}
