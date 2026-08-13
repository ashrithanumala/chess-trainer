import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Chess } from 'chess.js'
import Board from './Board'
import { api } from './api'

const CLASS_COLOR = {
  blunder: '#c53030',
  mistake: '#d97706',
  inaccuracy: '#b7791f',
  good: '#5a6572',
  best: '#2f855a',
  forced: '#6b7785',
  book: '#6b7785',
}
const CLASS_GLYPH = {
  blunder: '??', mistake: '?', inaccuracy: '?!', best: '!', good: '', forced: '', book: '',
}

function destsOf(chess) {
  const map = new Map()
  for (const m of chess.moves({ verbose: true })) {
    if (!map.has(m.from)) map.set(m.from, [])
    map.get(m.from).push(m.to)
  }
  return map
}

function fmtEval(p) {
  if (!p) return '—'
  if (p.mate_after != null) return `M${Math.abs(p.mate_after)}`
  if (p.cp_after == null) return '—'
  return (p.cp_after / 100).toFixed(1)
}

/** cp from white's POV, for the bar and the graph. */
function whiteCp(p) {
  if (!p) return 0
  let cp = p.cp_after ?? 0
  // mate_after === 0 means this move delivered mate, which is good for the mover.
  if (p.mate_after != null) cp = p.mate_after >= 0 ? 1000 : -1000
  return p.side === 'white' ? cp : -cp
}

function EvalBar({ cp }) {
  const clamped = Math.max(-800, Math.min(800, cp))
  const whitePct = 50 + (clamped / 800) * 50
  return (
    <div className="evalbar" title={`${(cp / 100).toFixed(2)} (white POV)`}>
      <div className="evalbar-white" style={{ height: `${whitePct}%` }} />
    </div>
  )
}

function EvalGraph({ positions, ply, onSeek, myColor }) {
  const w = 100, h = 100
  if (!positions.length) return null
  const pts = positions.map((p, i) => {
    const cp = whiteCp(p) * (myColor === 'black' ? -1 : 1)
    const y = h / 2 - (Math.max(-800, Math.min(800, cp)) / 800) * (h / 2)
    return `${(i / Math.max(1, positions.length - 1)) * w},${y}`
  })
  return (
    <svg className="evalgraph" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
         onClick={(e) => {
           const rect = e.currentTarget.getBoundingClientRect()
           const frac = (e.clientX - rect.left) / rect.width
           onSeek(Math.round(frac * positions.length))
         }}>
      <line x1="0" y1={h / 2} x2={w} y2={h / 2} stroke="#c9d1da" strokeWidth="0.5" />
      <polyline points={pts.join(' ')} fill="none" stroke="#2b6cb0" strokeWidth="1.2"
                vectorEffect="non-scaling-stroke" />
      {positions.map((p, i) =>
        ['blunder', 'mistake'].includes(p.class) ? (
          <circle key={i} cx={(i / Math.max(1, positions.length - 1)) * w}
                  cy={h / 2 - (Math.max(-800, Math.min(800, whiteCp(p) * (myColor === 'black' ? -1 : 1))) / 800) * (h / 2)}
                  r="1.5" fill={CLASS_COLOR[p.class]} />
        ) : null
      )}
      <line x1={(ply / Math.max(1, positions.length)) * w} y1="0"
            x2={(ply / Math.max(1, positions.length)) * w} y2={h}
            stroke="#e0524a" strokeWidth="0.6" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

export default function Review({ gameId, onBack }) {
  const [data, setData] = useState(null)
  const [ply, setPly] = useState(0)
  const [branch, setBranch] = useState(null)   // {fen, history:[{san,uci}], startPly}
  const [coachMode, setCoachMode] = useState(false)
  const [quiz, setQuiz] = useState(null)       // {ply, tries} while waiting for a better move
  const [quizMsg, setQuizMsg] = useState(null)
  const [playVsEngine, setPlayVsEngine] = useState(false)
  const [liveEval, setLiveEval] = useState(null)
  const [noteDraft, setNoteDraft] = useState('')
  const [tagDraft, setTagDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const boardRef = useRef(null)

  const load = useCallback(async () => {
    setData(await api.game(gameId))
  }, [gameId])

  useEffect(() => { load() }, [load])
  useEffect(() => { setPly(0); setBranch(null); setQuiz(null) }, [gameId])

  const positions = data?.positions ?? []
  const game = data?.game
  const myColor = game?.my_color ?? 'white'

  // Final position: replay the PGN once so we have a fen for "after last move".
  const finalFen = useMemo(() => {
    if (!positions.length) return new Chess().fen()
    const last = positions[positions.length - 1]
    const c = new Chess(last.fen)
    try { c.move(last.san) } catch { /* malformed pgn, fall back to last fen */ }
    return c.fen()
  }, [positions])

  const mainFen = ply < positions.length ? positions[ply].fen : finalFen
  const currentFen = branch ? branch.fen : mainFen
  const lastPos = ply > 0 ? positions[ply - 1] : null

  const chess = useMemo(() => new Chess(currentFen), [currentFen])
  const dests = useMemo(() => destsOf(chess), [chess])
  const turnColor = chess.turn() === 'w' ? 'white' : 'black'

  const lastMove = useMemo(() => {
    if (branch) {
      const h = branch.history[branch.history.length - 1]
      return h ? [h.from, h.to] : undefined
    }
    return lastPos ? [lastPos.uci.slice(0, 2), lastPos.uci.slice(2, 4)] : undefined
  }, [branch, lastPos])

  // Arrow for the engine's preferred move at the current position.
  const shapes = useMemo(() => {
    if (branch) {
      if (!liveEval?.lines?.[0]?.best_uci) return []
      const u = liveEval.lines[0].best_uci
      return [{ orig: u.slice(0, 2), dest: u.slice(2, 4), brush: 'paleBlue' }]
    }
    const p = positions[ply]
    if (!p?.best_uci) return []
    if (quiz) return []                            // don't spoil the quiz
    return [{ orig: p.best_uci.slice(0, 2), dest: p.best_uci.slice(2, 4), brush: 'paleGreen' }]
  }, [branch, liveEval, positions, ply, quiz])

  // Live eval while exploring a branch.
  useEffect(() => {
    if (!branch) { setLiveEval(null); return }
    let cancelled = false
    api.evalFen(branch.fen, 14, 2).then((r) => { if (!cancelled) setLiveEval(r) }).catch(() => {})
    return () => { cancelled = true }
  }, [branch?.fen])

  const goto = useCallback((n) => {
    setBranch(null)
    setQuiz(null)
    setQuizMsg(null)
    setPly(Math.max(0, Math.min(positions.length, n)))
  }, [positions.length])

  // Coach mode: pause before replaying one of my bad moves and ask for better.
  const stepForward = useCallback(() => {
    if (branch) return
    const p = positions[ply]
    if (!p) return
    if (coachMode && !quiz && p.side === myColor &&
        ['blunder', 'mistake'].includes(p.class)) {
      setQuiz({ ply, tries: 0 })
      setQuizMsg(`Your move here was a ${p.class}. Find something better on the board.`)
      return
    }
    setQuiz(null)
    setQuizMsg(null)
    setPly(ply + 1)
  }, [branch, positions, ply, coachMode, quiz, myColor])

  useEffect(() => {
    const onKey = (e) => {
      if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return
      if (e.key === 'ArrowRight') { e.preventDefault(); stepForward() }
      if (e.key === 'ArrowLeft') { e.preventDefault(); branch ? undoBranch() : goto(ply - 1) }
      if (e.key === 'ArrowUp') { e.preventDefault(); goto(0) }
      if (e.key === 'ArrowDown') { e.preventDefault(); goto(positions.length) }
      if (e.key === 'f') setFlip((f) => !f)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const [flip, setFlip] = useState(false)
  const orientation = flip ? (myColor === 'white' ? 'black' : 'white') : myColor

  // With "play vs engine" on, the engine owns the other side outright and moves
  // whenever it is that side's turn — on the main line as well as inside a
  // branch. Without the main-line case, toggling this on at a position where the
  // opponent is to move leaves the board frozen with nothing able to move it.
  useEffect(() => {
    if (!playVsEngine || quiz) return
    const fen = currentFen
    const c = new Chess(fen)
    if (c.isGameOver()) return
    if ((c.turn() === 'w' ? 'white' : 'black') === myColor) return
    let cancelled = false
    setBusy(true)
    api.enginePlay(fen, 8, 300)
      .then((res) => {
        if (cancelled || !res.move) return
        const step = {
          san: res.move.san,
          from: res.move.uci.slice(0, 2),
          to: res.move.uci.slice(2, 4),
          byEngine: true,
        }
        setBranch((b) => {
          if (b) {
            return b.fen === fen
              ? { ...b, fen: res.fen, history: [...b.history, step] }
              : b
          }
          // Engine moved us off the game's main line; start a branch there.
          return { fen: res.fen, history: [step], startPly: ply }
        })
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) setBusy(false) })
    return () => { cancelled = true }
  }, [currentFen, playVsEngine, myColor, quiz, ply])

  const onMove = useCallback(async (from, to) => {
    const c = new Chess(currentFen)
    let move
    try {
      move = c.move({ from, to, promotion: 'q' })
    } catch { return }
    if (!move) return

    // Quiz: judge the attempt against the engine's choice for this position.
    if (quiz && !branch) {
      const p = positions[quiz.ply]
      const played = from + to
      if (p.best_uci && played === p.best_uci.slice(0, 4)) {
        setQuizMsg(`Correct — ${move.san} was the engine's pick. ${p.pv ? 'Line: ' + p.pv : ''}`)
        setQuiz(null)
        setPly(quiz.ply + 1)
        return
      }
      setBusy(true)
      try {
        const r = await api.evalFen(c.fen(), 14, 1)
        const line = r.lines?.[0]
        const cpForMover = line?.cp != null ? -line.cp : null
        const better = cpForMover != null && cpForMover > (p.cp_after ?? -9999) + 30
        setQuizMsg(
          better
            ? `${move.san} is better than what you played (${p.san}), though the engine likes ${p.best_san}.`
            : `${move.san} doesn't fix it either. Engine wants ${p.best_san}.`
        )
      } finally { setBusy(false) }
      setQuiz({ ...quiz, tries: quiz.tries + 1 })
      return
    }

    // On the main line, playing the game's actual move just steps forward.
    if (!branch && positions[ply] && from + to === positions[ply].uci.slice(0, 4)) {
      setPly(ply + 1)
      return
    }

    const history = [
      ...(branch?.history ?? []),
      { san: move.san, from, to, byEngine: false },
    ]
    setBranch({ fen: c.fen(), history, startPly: branch?.startPly ?? ply })
  }, [currentFen, branch, positions, ply, quiz])

  const undoBranch = useCallback(() => {
    if (!branch) return
    // Against the engine, take back the pair so you land back on your own move
    // instead of on a turn the engine would instantly play again.
    const drop = playVsEngine && branch.history[branch.history.length - 1]?.byEngine ? -2 : -1
    const hist = branch.history.slice(0, drop)
    if (!hist.length) { setBranch(null); return }
    const c = new Chess(mainFen)
    for (const m of hist) c.move({ from: m.from, to: m.to, promotion: 'q' })
    setBranch({ ...branch, fen: c.fen(), history: hist })
  }, [branch, mainFen, playVsEngine])

  const comments = lastPos ? (data?.annotations?.[ply - 1] ?? []) : []

  const runExplain = async () => {
    setBusy(true)
    try {
      await api.explain(gameId)
      await load()
    } catch (e) {
      setQuizMsg(String(e.message || e).includes('ANTHROPIC_API_KEY')
        ? 'Set ANTHROPIC_API_KEY in the server environment to enable written coaching.'
        : 'Explain failed: ' + e.message)
    } finally { setBusy(false) }
  }

  const addNote = async () => {
    if (!noteDraft.trim()) return
    await api.addNote(gameId, noteDraft.trim(), ply > 0 ? ply - 1 : null)
    setNoteDraft('')
    load()
  }
  const addTag = async () => {
    if (!tagDraft.trim()) return
    await api.addTag(gameId, tagDraft.trim())
    setTagDraft('')
    load()
  }

  const runAnalysis = async () => {
    setBusy(true)
    try { await api.analyze(gameId, 16); await load() } finally { setBusy(false) }
  }

  if (!data) return <div className="pad">loading…</div>

  const barCp = branch
    ? (liveEval?.lines?.[0]?.cp ?? 0) * (turnColor === 'white' ? 1 : -1)
    : whiteCp(lastPos)

  return (
    <div className="review">
      <div className="review-top">
        <button onClick={onBack}>← library</button>
        <div className="players">
          <b>{game.white}</b> ({game.white_elo}) vs <b>{game.black}</b> ({game.black_elo})
          <span className="dim"> · {game.time_class} · {game.eco} {game.opening}</span>
        </div>
        <div className="spacer" />
        {!game.analyzed_at ? (
          <button onClick={runAnalysis} disabled={busy}>
            {busy ? 'analyzing…' : 'analyze game'}
          </button>
        ) : (
          <button onClick={runExplain} disabled={busy} title="LLM commentary for your errors">
            {busy ? 'working…' : 'explain in words'}
          </button>
        )}
      </div>

      <div className="review-body">
        <div className="board-col">
          <div className="board-row">
            <EvalBar cp={barCp} />
            <Board
              fen={currentFen}
              orientation={orientation}
              lastMove={lastMove}
              dests={dests}
              turnColor={turnColor}
              movableColor={playVsEngine ? myColor : turnColor}
              onMove={onMove}
              shapes={shapes}
              check={chess.inCheck()}
            />
          </div>

          <div className="controls">
            <button onClick={() => goto(0)}>⏮</button>
            <button onClick={() => (branch ? undoBranch() : goto(ply - 1))}>◀</button>
            <button onClick={stepForward}>▶</button>
            <button onClick={() => goto(positions.length)}>⏭</button>
            <button onClick={() => setFlip((f) => !f)}>flip</button>
            <label className="toggle">
              <input type="checkbox" checked={coachMode}
                     onChange={(e) => { setCoachMode(e.target.checked); setQuiz(null) }} />
              coach mode
            </label>
            <label className="toggle">
              <input type="checkbox" checked={playVsEngine}
                     onChange={(e) => setPlayVsEngine(e.target.checked)} />
              play vs engine
            </label>
            {branch && <button onClick={() => setBranch(null)}>back to game</button>}
          </div>

          <EvalGraph positions={positions} ply={ply} onSeek={goto} myColor={myColor} />
        </div>

        <div className="side-col">
          {quizMsg && <div className="coach quiz">{quizMsg}</div>}

          {branch ? (
            <div className="panel">
              <h3>exploring {playVsEngine ? '(engine replies)' : '(free moves)'}</h3>
              <div className="branch-line">
                {branch.history.map((m, i) => (
                  <span key={i} className={m.byEngine ? 'dim' : ''}>{m.san} </span>
                ))}
              </div>
              {liveEval?.lines?.[0] && (
                <div className="dim">
                  eval {liveEval.lines[0].mate != null
                    ? `M${Math.abs(liveEval.lines[0].mate)}`
                    : (liveEval.lines[0].cp / 100).toFixed(2)} ·
                  best {liveEval.lines[0].best_san}
                  {liveEval.lines[0].pv ? ` · ${liveEval.lines[0].pv}` : ''}
                </div>
              )}
              <button onClick={undoBranch}>undo move</button>
              <button onClick={() => setBranch(null)}>return to game line</button>
            </div>
          ) : (
            comments.length > 0 && (
              <div className="coach">
                {comments.map((c, i) => (
                  <p key={i} className={c.source === 'llm' ? 'llm' : ''}>
                    {c.source === 'llm' && <span className="dim">coach · </span>}
                    {c.body}
                  </p>
                ))}
              </div>
            )
          )}

          <div className="panel movelist">
            <h3>moves</h3>
            <div className="moves">
              {positions.map((p, i) => (
                <span key={i}>
                  {p.side === 'white' && <span className="movenum">{p.move_no}.</span>}
                  <button
                    className={'move' + (ply === i + 1 ? ' current' : '')}
                    style={{ color: CLASS_COLOR[p.class] }}
                    title={`${p.class} · ${fmtEval(p)}${p.best_san ? ' · best ' + p.best_san : ''}`}
                    onClick={() => goto(i + 1)}
                  >
                    {p.san}{CLASS_GLYPH[p.class]}
                  </button>
                </span>
              ))}
            </div>
          </div>

          <div className="panel">
            <h3>tags</h3>
            <div className="chips">
              {data.tags.map((t) => (
                <span key={t.tag + t.auto} className={'chip' + (t.auto ? ' auto' : '')}>
                  {t.tag}
                  {!t.auto && (
                    <button onClick={async () => { await api.removeTag(gameId, t.tag); load() }}>×</button>
                  )}
                </span>
              ))}
            </div>
            <div className="row">
              <input value={tagDraft} onChange={(e) => setTagDraft(e.target.value)}
                     placeholder="add tag" onKeyDown={(e) => e.key === 'Enter' && addTag()} />
              <button onClick={addTag}>add</button>
            </div>
          </div>

          <div className="panel">
            <h3>notes {ply > 0 && <span className="dim">(attaches to move {ply})</span>}</h3>
            {data.notes.map((n) => (
              <div key={n.id} className="note">
                {n.ply != null && (
                  <button className="linkish" onClick={() => goto(n.ply + 1)}>
                    @{positions[n.ply]?.move_no}{positions[n.ply]?.side === 'white' ? '.' : '...'}
                    {positions[n.ply]?.san}
                  </button>
                )}
                <span>{n.body}</span>
                <button className="linkish"
                        onClick={async () => { await api.deleteNote(n.id); load() }}>delete</button>
              </div>
            ))}
            <textarea value={noteDraft} onChange={(e) => setNoteDraft(e.target.value)}
                      placeholder="what happened here?" rows={2} />
            <button onClick={addNote}>save note</button>
          </div>
        </div>
      </div>
    </div>
  )
}
