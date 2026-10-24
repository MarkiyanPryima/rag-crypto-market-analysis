import { useState } from 'react'
import { QueryScreen } from './QueryScreen'
import { StatsScreen } from './StatsScreen'

type Tab = 'query' | 'stats'

export default function App() {
  const [tab, setTab] = useState<Tab>('query')
  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <strong>Crypto RAG</strong>
          <span className="muted">аналіз ринку BTC/ETH: новини + ринкові дані</span>
        </div>
        <nav className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === 'query'} className={tab === 'query' ? 'on' : ''} onClick={() => setTab('query')}>
            Запит
          </button>
          <button role="tab" aria-selected={tab === 'stats'} className={tab === 'stats' ? 'on' : ''} onClick={() => setTab('stats')}>
            Статистика
          </button>
        </nav>
      </header>
      <main>{tab === 'query' ? <QueryScreen /> : <StatsScreen />}</main>
    </div>
  )
}
