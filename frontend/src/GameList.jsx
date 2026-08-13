import { useEffect, useState } from 'react'
import { api } from './api'

const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleDateString() : '')

export default function GameList({ onOpen }) {
  const [filters, setFilters] = useState({ class: 'rapid', result: '', color: '', tag: '', q: '' })
  const [data, setData] = useState({ total: 0, games: [] })
  const [tags, setTags] = useState([])
  const [loading, setLoading] = useState(false)

  useEffect(() => { api.tags().then(setTags).catch(() => {}) }, [])

  useEffect(() => {
    setLoading(true)
    const params = Object.fromEntries(Object.entries(filters).filter(([, v]) => v))
    api.games({ ...params, limit: 200 })
      .then(setData)
      .finally(() => setLoading(false))
  }, [filters])

  const set = (k) => (e) => setFilters((f) => ({ ...f, [k]: e.target.value }))

  return (
    <div className="pad">
      <div className="filters">
        <select value={filters.class} onChange={set('class')}>
          <option value="">all speeds</option>
          <option value="rapid">rapid</option>
          <option value="blitz">blitz</option>
          <option value="bullet">bullet</option>
          <option value="daily">daily</option>
        </select>
        <select value={filters.result} onChange={set('result')}>
          <option value="">any result</option>
          <option value="win">wins</option>
          <option value="loss">losses</option>
          <option value="draw">draws</option>
        </select>
        <select value={filters.color} onChange={set('color')}>
          <option value="">both colors</option>
          <option value="white">as white</option>
          <option value="black">as black</option>
        </select>
        <select value={filters.tag} onChange={set('tag')}>
          <option value="">any tag</option>
          {tags.map((t) => (
            <option key={t.tag + t.auto} value={t.tag}>
              {t.tag} ({t.count}){t.auto ? '' : ' ✎'}
            </option>
          ))}
        </select>
        <input value={filters.q} onChange={set('q')} placeholder="search opening, opponent, notes" />
        <span className="dim">{loading ? 'loading…' : `${data.total} games`}</span>
      </div>

      <table className="games">
        <thead>
          <tr>
            <th>date</th><th>color</th><th>opponent</th><th>result</th>
            <th>opening</th><th>acpl</th><th>?? / ?</th><th>tags</th>
          </tr>
        </thead>
        <tbody>
          {data.games.map((g) => {
            const opp = g.my_color === 'white' ? g.black : g.white
            const oppElo = g.my_color === 'white' ? g.black_elo : g.white_elo
            const acpl = g.my_color === 'white' ? g.acpl_white : g.acpl_black
            return (
              <tr key={g.id} onClick={() => onOpen(g.id)} className={'r-' + g.my_result}>
                <td>{fmtDate(g.played_at)}</td>
                <td>{g.my_color === 'white' ? '□' : '■'}</td>
                <td>{opp} <span className="dim">{oppElo}</span></td>
                <td className={'res ' + g.my_result}>{g.my_result}</td>
                <td className="dim">{g.eco} {g.opening}</td>
                <td>{acpl ?? <span className="dim">—</span>}</td>
                <td>
                  {g.analyzed_at
                    ? <>
                        <span style={{ color: '#c53030' }}>{g.my_blunders}</span>
                        {' / '}
                        <span style={{ color: '#d97706' }}>{g.my_mistakes}</span>
                      </>
                    : <span className="dim">unanalyzed</span>}
                </td>
                <td className="chips">
                  {g.tags.slice(0, 4).map((t) => <span key={t} className="chip auto">{t}</span>)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
