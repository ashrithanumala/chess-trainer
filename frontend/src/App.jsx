import { useState } from 'react'
import GameList from './GameList'
import Review from './Review'
import { Openings, SearchView, Stats } from './Reports'
import Play from './Play'
import Repertoire from './Repertoire'
import EngineStatus from './EngineStatus'
import Sync from './Sync'

const TABS = [
  ['library', 'library'],
  ['play', 'play'],
  ['openings', 'openings'],
  ['repertoire', 'fix my openings'],
  ['search', 'search'],
  ['stats', 'stats'],
]

export default function App() {
  const [tab, setTab] = useState('library')
  const [gameId, setGameId] = useState(null)
  const [refresh, setRefresh] = useState(0)

  const open = (id) => { setGameId(id); setTab('review') }

  return (
    <div className="app">
      <header>
        <span className="brand">chess trainer</span>
        {TABS.map(([k, label]) => (
          <button key={k} className={tab === k ? 'tab active' : 'tab'} onClick={() => setTab(k)}>
            {label}
          </button>
        ))}
        <span className="spacer" />
        <EngineStatus />
        <Sync onDone={() => setRefresh((n) => n + 1)} />
      </header>
      {tab === 'library' && <GameList key={refresh} onOpen={open} />}
      {tab === 'play' && <Play />}
      {tab === 'openings' && <Openings key={refresh} />}
      {tab === 'repertoire' && <Repertoire key={refresh} />}
      {tab === 'search' && <SearchView onOpen={open} />}
      {tab === 'stats' && <Stats />}
      {tab === 'review' && gameId && (
        <Review gameId={gameId} onBack={() => setTab('library')} />
      )}
    </div>
  )
}
