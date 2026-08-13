import { useEffect, useState } from 'react'
import { api } from './api'

export function Openings() {
  const [rows, setRows] = useState([])
  const [cls, setCls] = useState('rapid')
  useEffect(() => {
    api.openings({ class: cls, min_games: 2 }).then(setRows).catch(() => setRows([]))
  }, [cls])

  return (
    <div className="pad">
      <div className="filters">
        <select value={cls} onChange={(e) => setCls(e.target.value)}>
          <option value="">all speeds</option>
          <option value="rapid">rapid</option>
          <option value="blitz">blitz</option>
          <option value="bullet">bullet</option>
        </select>
        <span className="dim">
          score% is raw result. opening acpl / errors are engine-measured, and only
          count moves in the first 10 moves — that is the part the opening owns.
        </span>
      </div>
      <table className="games">
        <thead>
          <tr>
            <th>eco</th><th>opening</th><th>color</th><th>games</th>
            <th>score%</th><th>opening acpl</th><th>opening errors</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td>{r.eco}</td>
              <td>{r.opening}</td>
              <td>{r.my_color === 'white' ? '□' : '■'}</td>
              <td>{r.games}</td>
              <td style={{ color: r.score >= 55 ? '#2f855a' : r.score <= 45 ? '#c53030' : undefined }}>
                {r.score}
              </td>
              <td>{r.opening_acpl ?? '—'}</td>
              <td>{r.opening_errors ?? 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function SearchView({ onOpen }) {
  const [q, setQ] = useState('')
  const [rows, setRows] = useState([])
  const [ran, setRan] = useState(false)

  const run = async () => {
    if (!q.trim()) return
    try { setRows(await api.search(q.trim())) } catch { setRows([]) }
    setRan(true)
  }

  return (
    <div className="pad">
      <div className="filters">
        <input value={q} onChange={(e) => setQ(e.target.value)} style={{ minWidth: 320 }}
               placeholder="search notes and coach commentary — e.g. hangs, forced mate, pin"
               onKeyDown={(e) => e.key === 'Enter' && run()} />
        <button onClick={run}>search</button>
      </div>
      {ran && !rows.length && <p className="dim">nothing matched.</p>}
      {rows.map((r, i) => (
        <div key={i} className="hit" onClick={() => onOpen(r.game_id)}>
          <div className="dim">
            {r.kind} · {r.white} vs {r.black} · {r.opening} · {r.my_result}
          </div>
          <div dangerouslySetInnerHTML={{ __html: r.excerpt }} />
        </div>
      ))}
    </div>
  )
}

export function Stats() {
  const [s, setS] = useState(null)
  useEffect(() => { api.stats().then(setS).catch(() => {}) }, [])
  if (!s) return <div className="pad">loading…</div>

  const byClass = {}
  for (const m of s.move_classes) {
    byClass[m.class] = (byClass[m.class] || 0) + m.n
  }
  const totalMoves = Object.values(byClass).reduce((a, b) => a + b, 0)

  return (
    <div className="pad">
      <h3>library</h3>
      <p>
        {s.totals.games} games · {s.totals.analyzed} analyzed ·
        {' '}{s.totals.wins}W {s.totals.losses}L {s.totals.draws}D
      </p>
      <h3>by speed</h3>
      <table className="games">
        <thead><tr><th>speed</th><th>games</th><th>wins</th><th>avg acpl</th></tr></thead>
        <tbody>
          {s.by_class.map((r) => (
            <tr key={r.time_class}>
              <td>{r.time_class}</td><td>{r.n}</td><td>{r.wins}</td><td>{r.acpl ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>your move quality ({totalMoves} analyzed moves)</h3>
      <table className="games">
        <thead><tr><th>class</th><th>opening</th><th>middlegame</th><th>endgame</th></tr></thead>
        <tbody>
          {['best', 'good', 'inaccuracy', 'mistake', 'blunder', 'forced'].map((c) => {
            const get = (ph) => s.move_classes.find((m) => m.class === c && m.phase === ph)?.n ?? 0
            return (
              <tr key={c}>
                <td>{c}</td><td>{get('opening')}</td><td>{get('middlegame')}</td><td>{get('endgame')}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
