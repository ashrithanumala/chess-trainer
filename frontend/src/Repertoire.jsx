import { useEffect, useMemo, useState } from 'react'
import { Chess } from 'chess.js'
import Board from './Board'
import { api } from './api'

/**
 * Opening leaks: positions from your own games where you bleed eval or score
 * badly, with concrete replacement moves from the engine and (when a Lichess
 * token is configured) from what your rating band actually plays.
 */
const whitePovCp = (cp, sideToMove) => (cp == null ? null : sideToMove === 'w' ? cp : -cp)

export default function Repertoire() {
  const [color, setColor] = useState('white')
  const [cls, setCls] = useState('rapid')
  const [data, setData] = useState(null)
  const [sel, setSel] = useState(0)
  const [loading, setLoading] = useState(false)
  const [drill, setDrill] = useState(null)   // {fen, history} playing the fix out
  const [liveEval, setLiveEval] = useState(null)
  const [thinking, setThinking] = useState(false)
  const [replyMode, setReplyMode] = useState('choose')  // 'choose' | 'engine'
  const [replies, setReplies] = useState(null)

  useEffect(() => {
    setLoading(true)
    setDrill(null)
    setSel(0)
    api.repertoire({ color, class: cls, min_games: 3 })
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [color, cls])

  const leak = data?.leaks?.[sel]
  const boardFen = drill ? drill.fen : leak?.fen
  const chess = useMemo(() => (boardFen ? new Chess(boardFen) : null), [boardFen])

  const oppToMove = !!chess && (chess.turn() === 'w' ? 'white' : 'black') !== color

  const dests = useMemo(() => {
    if (!chess) return new Map()
    const m = new Map()
    for (const mv of chess.moves({ verbose: true })) {
      if (!m.has(mv.from)) m.set(mv.from, [])
      m.get(mv.from).push(mv.to)
    }
    return m
  }, [chess])

  // Once you start drilling, the static advice belongs to the starting position,
  // so re-evaluate the position actually on the board.
  useEffect(() => {
    if (!drill) { setLiveEval(null); return }
    let cancelled = false
    setLiveEval(null)
    api.evalFen(drill.fen, 14, 3)
      .then((r) => { if (!cancelled) setLiveEval(r) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [drill?.fen])

  // Opponent replies for the position on the board, so you can pick what to face.
  useEffect(() => {
    if (!drill || !oppToMove || replyMode !== 'choose') { setReplies(null); return }
    let cancelled = false
    setReplies(null)
    api.replies({ fen: drill.fen, my_color: color })
      .then((r) => { if (!cancelled) setReplies(r) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [drill?.fen, oppToMove, replyMode, color])

  // Green = recommended, red = what you actually play. While drilling, blue is
  // the engine's move for the position in front of you.
  const shapes = useMemo(() => {
    if (drill) {
      const u = liveEval?.lines?.[0]?.best_uci
      return u ? [{ orig: u.slice(0, 2), dest: u.slice(2, 4), brush: 'paleBlue' }] : []
    }
    if (!leak) return []
    const out = []
    const mine = leak.my_moves?.[0]
    if (mine?.uci) {
      out.push({ orig: mine.uci.slice(0, 2), dest: mine.uci.slice(2, 4), brush: 'red' })
    }
    for (const r of leak.recommendations || []) {
      if (r.uci) out.push({ orig: r.uci.slice(0, 2), dest: r.uci.slice(2, 4), brush: 'green' })
    }
    return out
  }, [leak, drill])

  // You only ever move your own pieces. The opponent's move comes from the
  // engine or from the reply list, never from you.
  const onMove = (from, to) => {
    if (!chess || oppToMove) return
    const c = new Chess(boardFen)
    let mv
    try { mv = c.move({ from, to, promotion: 'q' }) } catch { return }
    if (!mv) return
    setDrill({
      fen: c.fen(),
      history: [...(drill?.history ?? []), { san: mv.san, byEngine: false }],
    })
  }

  // Engine answers whenever it is the opponent's turn — including after a take
  // back that lands there — instead of only right after one of your moves.
  useEffect(() => {
    if (!drill || replyMode !== 'engine' || !oppToMove) return
    const c = new Chess(drill.fen)
    if (c.isGameOver()) return
    let cancelled = false
    setThinking(true)
    api.enginePlay(drill.fen, 8, 300)
      .then((res) => {
        if (cancelled || !res.move) return
        setDrill((d) => (d && d.fen === drill.fen
          ? { fen: res.fen, history: [...d.history, { san: res.move.san, byEngine: true }] }
          : d))
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) setThinking(false) })
    return () => { cancelled = true }
  }, [drill?.fen, replyMode, oppToMove])

  const playReply = (san) => {
    if (!drill) return
    const c = new Chess(drill.fen)
    let mv
    try { mv = c.move(san) } catch { return }
    if (!mv) return
    setDrill({
      fen: c.fen(),
      history: [...drill.history, { san: mv.san, byEngine: true }],
    })
  }

  const undoDrill = () => {
    if (!drill || !leak) return
    // Drop the last pair (yours + the engine's reply) back off the line.
    const hist = drill.history.slice(0, drill.history[drill.history.length - 1]?.byEngine ? -2 : -1)
    if (!hist.length) { setDrill(null); return }
    const c = new Chess(leak.fen)
    for (const m of hist) c.move(m.san)
    setDrill({ fen: c.fen(), history: hist })
  }

  return (
    <div className="pad">
      <div className="filters">
        <select value={color} onChange={(e) => setColor(e.target.value)}>
          <option value="white">as white</option>
          <option value="black">as black</option>
        </select>
        <select value={cls} onChange={(e) => setCls(e.target.value)}>
          <option value="">all speeds</option>
          <option value="rapid">rapid</option>
          <option value="blitz">blitz</option>
          <option value="bullet">bullet</option>
        </select>
        {loading && <span className="dim">analyzing your tree…</span>}
        {data && !data.explorer_available && (
          <span className="dim">
            human win-rate data off — set LICHESS_TOKEN to enable; engine advice still shown
          </span>
        )}
      </div>

      {!loading && !data?.leaks?.length && (
        <p className="dim">
          No leaks found for this filter. Needs at least 3 analyzed games reaching the
          same position — analyze more games, or widen the speed filter.
        </p>
      )}

      {!!data?.leaks?.length && (
        <div className="rep-body">
          <div className="rep-list">
            {data.leaks.map((l, i) => (
              <div key={l.key} className={'rep-item' + (i === sel ? ' active' : '')}
                   onClick={() => { setSel(i); setDrill(null) }}>
                <div className="rep-head">
                  move {l.move_no} · {l.games} games
                  <span className="spacer" />
                  <span style={{ color: l.avg_cp_loss >= 60 ? '#c53030' : '#d97706' }}>
                    {l.avg_cp_loss} acpl
                  </span>
                </div>
                <div className="dim">{l.eco} {l.opening}</div>
                <div className="dim">
                  you play {l.my_moves?.[0]?.san} · score {l.score}%
                </div>
              </div>
            ))}
          </div>

          <div className="rep-detail">
            <div className="rep-board">
              <Board
                fen={boardFen}
                orientation={color}
                dests={dests}
                turnColor={chess?.turn() === 'w' ? 'white' : 'black'}
                movableColor={color}
                onMove={onMove}
                shapes={shapes}
              />
            </div>

            <div className="rep-info">
              {drill ? (
                <div className="panel">
                  <h3>
                    drilling
                    {liveEval?.lines?.[0] && (
                      <span className="dim">
                        {' '}· eval {liveEval.lines[0].mate != null
                          ? `M${Math.abs(liveEval.lines[0].mate)}`
                          : (whitePovCp(liveEval.lines[0].cp, chess?.turn()) / 100).toFixed(2)}
                        <span className="dim"> (white)</span>
                      </span>
                    )}
                    {thinking && <span className="dim"> · engine thinking…</span>}
                  </h3>
                  <div className="branch-line">
                    {drill.history.map((m, i) => (
                      <span key={i} className={m.byEngine ? 'dim' : ''}>{m.san} </span>
                    ))}
                  </div>
                  <div className="row">
                    <button onClick={undoDrill}>take back</button>
                    <button onClick={() => setDrill(null)}>reset position</button>
                    <span className="spacer" />
                    <select value={replyMode} onChange={(e) => setReplyMode(e.target.value)}
                            title="who chooses the opponent's move">
                      <option value="choose">I pick the reply</option>
                      <option value="engine">engine replies</option>
                    </select>
                  </div>

                  {oppToMove && replyMode === 'choose' && (
                    <div className="replies">
                      <h3>opponent replies</h3>
                      {!replies && <span className="dim">looking up…</span>}

                      {!!replies?.mine?.length && (
                        <>
                          <div className="dim small">from your own games</div>
                          {replies.mine.map((m) => (
                            <button key={'m' + m.san} className="reply"
                                    onClick={() => playReply(m.san)}>
                              <b>{m.san}</b>
                              <span className="dim"> faced {m.n}x</span>
                              <span className="spacer" />
                              <span style={{ color: m.my_score >= 50 ? '#2f855a' : '#c53030' }}>
                                you score {m.my_score}%
                              </span>
                            </button>
                          ))}
                        </>
                      )}

                      {!!replies?.book?.length && (
                        <>
                          <div className="dim small">common at your rating</div>
                          {replies.book.map((b) => (
                            <button key={'b' + b.san} className="reply"
                                    onClick={() => playReply(b.san)}>
                              <b>{b.san}</b>
                              <span className="dim"> {b.popularity}% of games</span>
                              <span className="spacer" />
                              <span className="dim">you'd score {b.my_score}%</span>
                            </button>
                          ))}
                        </>
                      )}

                      {!!replies?.engine?.filter((e) => e.san).length && (
                        <>
                          <div className="dim small">engine's best (hardest test)</div>
                          {replies.engine.filter((e) => e.san).map((e) => (
                            <button key={'e' + e.san} className="reply"
                                    onClick={() => playReply(e.san)}>
                              <b>{e.san}</b>
                              <span className="spacer" />
                              <span className="dim">
                                {e.cp != null
                                  ? (whitePovCp(e.cp, chess?.turn()) / 100).toFixed(2)
                                  : `M${Math.abs(e.mate)}`}
                              </span>
                            </button>
                          ))}
                        </>
                      )}

                      {replies && !replies.mine?.length && !replies.book?.length && (
                        <div className="dim small">
                          No human data for this position — you have never reached it
                          {replies.explorer_error === 'explorer_unauthorized'
                            ? ' and the explorer needs a LICHESS_TOKEN.'
                            : '.'} Engine moves above still work.
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ) : (
                <div className="panel">
                  <h3>what you do here</h3>
                  {leak.my_moves.map((m) => (
                    <div key={m.san} className="rep-row">
                      <b>{m.san}</b> <span className="dim">x{m.n}</span>
                      <span className="spacer" />
                      <span>{m.avg_cp_loss} acpl</span>
                      <span className="dim"> · {m.score}%</span>
                      {m.errors > 0 && <span style={{ color: '#c53030' }}> · {m.errors} bad</span>}
                    </div>
                  ))}
                </div>
              )}

              {!drill && (
              <div className="panel">
                <h3>play instead</h3>
                {leak.recommendations?.length ? leak.recommendations.map((r) => (
                  <div key={r.san} className="rep-row">
                    <b style={{ color: '#2f855a' }}>{r.san}</b>
                    <span className="spacer" />
                    {r.score != null && <span>{r.score}% at your rating</span>}
                    {r.cp != null && <span className="dim"> eval {(r.cp / 100).toFixed(2)}</span>}
                  </div>
                )) : <span className="dim">no clear alternative</span>}
                {leak.recommendations?.[0]?.why && (
                  <div className="dim small">{leak.recommendations[0].why}</div>
                )}
                <div className="dim small">
                  Make a move on the board to play the position out against the engine.
                </div>
              </div>
              )}

              {/* Engine lines always describe the position currently on the board. */}
              {drill ? (
                <div className="panel">
                  <h3>engine lines <span className="dim">· current position</span></h3>
                  {liveEval?.lines?.length
                    ? liveEval.lines.filter((e) => e.best_san).map((e, i) => (
                        <div key={i} className="rep-row small">
                          <b>{e.best_san}</b>
                          <span className="dim">
                            {' '}{e.cp != null
                              ? (whitePovCp(e.cp, chess?.turn()) / 100).toFixed(2)
                              : `M${Math.abs(e.mate)}`}
                          </span>
                          <span className="dim"> · {e.pv}</span>
                        </div>
                      ))
                    : <span className="dim">evaluating…</span>}
                </div>
              ) : !!leak.engine?.length && (
                <div className="panel">
                  <h3>engine lines</h3>
                  {leak.engine.filter((e) => e.san).map((e) => (
                    <div key={e.san} className="rep-row small">
                      <b>{e.san}</b>
                      <span className="dim"> {e.cp != null ? (e.cp / 100).toFixed(2) : `M${e.mate}`}</span>
                      <span className="dim"> · {e.line}</span>
                    </div>
                  ))}
                </div>
              )}

              {!drill && !!leak.book?.length && (
                <div className="panel">
                  <h3>your rating band plays</h3>
                  {leak.book.map((b) => (
                    <div key={b.san} className="rep-row small">
                      <b>{b.san}</b>
                      <span className="spacer" />
                      <span>{b.popularity}% of games</span>
                      <span className="dim"> · scores {b.score}%</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
