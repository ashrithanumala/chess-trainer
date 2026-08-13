import { useEffect, useMemo, useState } from 'react'
import { Chess } from 'chess.js'
import Board from './Board'
import { api } from './api'

const START = new Chess().fen()

// Skill Level is Stockfish's own handicap knob; 20 plus more time = its best.
const LEVELS = [
  { key: 'best', label: 'best moves', skill: 20, ms: 800 },
  { key: 'hard', label: 'hard (~1600)', skill: 14, ms: 400 },
  { key: 'mid', label: 'medium (~1200)', skill: 8, ms: 300 },
  { key: 'easy', label: 'easy (~800)', skill: 3, ms: 200 },
]

/** Group into numbered rows, honouring a position that starts with black to move. */
function pairMoves(history, startFen) {
  const parts = startFen.split(' ')
  const blackFirst = parts[1] === 'b'
  const firstNo = parseInt(parts[5], 10) || 1
  const rows = []
  let i = 0
  if (blackFirst && history.length) {
    rows.push({ no: firstNo, w: null, b: history[0] })
    i = 1
  }
  for (; i < history.length; i += 2) {
    rows.push({
      no: firstNo + (blackFirst ? 1 : 0) + Math.floor((i - (blackFirst ? 1 : 0)) / 2),
      w: history[i],
      b: history[i + 1],
    })
  }
  return rows
}

/** Free play board: set up any position, choose a side, let the engine answer. */
export default function Play() {
  const [fen, setFen] = useState(START)
  const [startFen, setStartFen] = useState(START)   // what reset/undo rewind to
  const [history, setHistory] = useState([])
  const [myColor, setMyColor] = useState('white')
  const [engineOn, setEngineOn] = useState(true)
  const [level, setLevel] = useState('best')
  const [showBest, setShowBest] = useState(true)
  const [showEval, setShowEval] = useState(true)
  const [evaluation, setEvaluation] = useState(null)
  const [thinking, setThinking] = useState(false)
  const [fenDraft, setFenDraft] = useState('')
  const [fenError, setFenError] = useState(null)
  const [question, setQuestion] = useState('')
  const [thread, setThread] = useState([])       // [{q, a, moveNo, error}]
  const [asking, setAsking] = useState(false)

  const chess = useMemo(() => new Chess(fen), [fen])
  const turnColor = chess.turn() === 'w' ? 'white' : 'black'
  const oppToMove = turnColor !== myColor
  const gameOver = chess.isGameOver()
  const cfg = LEVELS.find((l) => l.key === level) ?? LEVELS[0]

  const dests = useMemo(() => {
    const m = new Map()
    for (const mv of chess.moves({ verbose: true })) {
      if (!m.has(mv.from)) m.set(mv.from, [])
      m.get(mv.from).push(mv.to)
    }
    return m
  }, [chess])

  const lastMove = useMemo(() => {
    const h = history[history.length - 1]
    return h ? [h.from, h.to] : undefined
  }, [history])

  // Evaluate the position whenever an arrow or a number is on screen.
  useEffect(() => {
    if (!showBest && !showEval) { setEvaluation(null); return }
    if (gameOver) { setEvaluation(null); return }
    let cancelled = false
    api.evalFen(fen, 16, 3)
      .then((r) => { if (!cancelled) setEvaluation(r) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [fen, showBest, showEval, gameOver])

  // The engine owns the other side entirely: it moves whenever it is that
  // side's turn, so you can never end up playing both.
  useEffect(() => {
    if (!engineOn || !oppToMove || gameOver) return
    let cancelled = false
    setThinking(true)
    api.enginePlay(fen, cfg.skill, cfg.ms)
      .then((res) => {
        if (cancelled || !res.move) return
        setFen(res.fen)
        setHistory((h) => [...h, {
          san: res.move.san,
          from: res.move.uci.slice(0, 2),
          to: res.move.uci.slice(2, 4),
          byEngine: true,
        }])
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) setThinking(false) })
    return () => { cancelled = true }
  }, [fen, engineOn, oppToMove, gameOver, cfg.skill, cfg.ms])

  const onMove = (from, to) => {
    if (gameOver) return
    if (engineOn && oppToMove) return          // engine's turn, hands off
    const c = new Chess(fen)
    let mv
    try { mv = c.move({ from, to, promotion: 'q' }) } catch { return }
    if (!mv) return
    setFen(c.fen())
    setHistory((h) => [...h, { san: mv.san, from, to, byEngine: false }])
  }

  const replay = (moves) => {
    const c = new Chess(startFen)
    for (const m of moves) c.move({ from: m.from, to: m.to, promotion: 'q' })
    setFen(c.fen())
    setHistory(moves)
  }

  const undo = () => {
    if (!history.length) return
    // Against the engine, drop the pair so you land back on your own move.
    const drop = engineOn && history[history.length - 1]?.byEngine ? -2 : -1
    replay(history.slice(0, drop))
  }

  const reset = () => replay([])

  const loadFen = () => {
    const raw = fenDraft.trim()
    if (!raw) return
    try {
      const c = new Chess(raw)
      setStartFen(c.fen())
      setFen(c.fen())
      setHistory([])
      setFenError(null)
      setFenDraft('')
    } catch {
      setFenError('not a valid FEN')
    }
  }

  const askCoach = async (text) => {
    const q = (text ?? question).trim()
    if (!q || asking) return
    setAsking(true)
    setQuestion('')
    // Send prior turns so follow-ups like "why not the other one?" make sense.
    const priorTurns = thread.filter((t) => t.a).map((t) => ({ q: t.q, a: t.a }))
    try {
      const res = await api.ask({
        fen,
        question: q,
        moves: history.map((m) => m.san),
        history: priorTurns,
      })
      setThread((t) => [...t, { q, a: res.answer, moveNo: chess.moveNumber() }])
    } catch (e) {
      setThread((t) => [...t, { q, error: String(e.message || e).slice(0, 200) }])
    } finally { setAsking(false) }
  }

  const SUGGESTED = [
    "What's my plan here?",
    'Why is the engine move best?',
    'What is my opponent threatening?',
    'What are the typical mistakes in this position?',
  ]

  const newGame = (color) => {
    setMyColor(color)
    setStartFen(START)
    setFen(START)
    setHistory([])
  }

  const top = evaluation?.lines?.[0]
  const shapes = useMemo(() => {
    if (!showBest || !top?.best_uci) return []
    return [{ orig: top.best_uci.slice(0, 2), dest: top.best_uci.slice(2, 4), brush: 'paleGreen' }]
  }, [showBest, top])

  // Engine evals are side-to-move relative; show them from White's side always.
  const whiteCp = top?.cp != null ? (chess.turn() === 'w' ? top.cp : -top.cp) : null
  const evalText = !top ? '—'
    : top.mate != null ? `M${Math.abs(top.mate)}`
    : (whiteCp / 100).toFixed(2)

  let status = `${turnColor} to move`
  if (chess.isCheckmate()) status = `checkmate — ${turnColor === 'white' ? 'black' : 'white'} wins`
  else if (chess.isStalemate()) status = 'stalemate'
  else if (chess.isDraw()) status = 'draw'
  else if (chess.inCheck()) status = `${turnColor} is in check`

  return (
    <div className="pad">
      <div className="filters">
        <button onClick={() => newGame('white')}>new game as white</button>
        <button onClick={() => newGame('black')}>new game as black</button>
        <button onClick={undo} disabled={!history.length}>take back</button>
        <button onClick={reset} disabled={!history.length}>reset position</button>
        <span className="spacer" />
        <span className="dim">{status}{thinking ? ' · engine thinking…' : ''}</span>
      </div>

      <div className="rep-detail">
        <div className="rep-board">
          <Board
            fen={fen}
            orientation={myColor}
            lastMove={lastMove}
            dests={dests}
            turnColor={turnColor}
            movableColor={engineOn ? myColor : turnColor}
            onMove={onMove}
            shapes={shapes}
            check={chess.inCheck()}
          />
        </div>

        <div className="rep-info">
          <div className="panel">
            <h3>opponent</h3>
            <label className="toggle">
              <input type="checkbox" checked={engineOn}
                     onChange={(e) => setEngineOn(e.target.checked)} />
              engine plays {myColor === 'white' ? 'black' : 'white'}
            </label>
            <div className="row">
              <select value={level} onChange={(e) => setLevel(e.target.value)}
                      disabled={!engineOn}>
                {LEVELS.map((l) => <option key={l.key} value={l.key}>{l.label}</option>)}
              </select>
            </div>
            <div className="dim small">
              Off: you move both sides freely. On: the engine owns the other side and
              you can only touch your own pieces.
            </div>
          </div>

          <div className="panel">
            <h3>coaching</h3>
            <label className="toggle">
              <input type="checkbox" checked={showBest}
                     onChange={(e) => setShowBest(e.target.checked)} />
              show best move (green arrow)
            </label>
            <label className="toggle">
              <input type="checkbox" checked={showEval}
                     onChange={(e) => setShowEval(e.target.checked)} />
              show evaluation and lines
            </label>
            {showEval && (
              <div className="rep-row">
                <b>eval {evalText}</b>
                <span className="dim"> (white)</span>
                <span className="spacer" />
                {top?.best_san && <span className="dim">best: {top.best_san}</span>}
              </div>
            )}
            {showEval && evaluation?.lines?.length > 1 && (
              <div className="small">
                {evaluation.lines.filter((l) => l.best_san).map((l, i) => (
                  <div key={i} className="rep-row small">
                    <b>{l.best_san}</b>
                    <span className="dim">
                      {' '}{l.cp != null
                        ? ((chess.turn() === 'w' ? l.cp : -l.cp) / 100).toFixed(2)
                        : `M${Math.abs(l.mate)}`}
                    </span>
                    <span className="dim"> · {l.pv}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="panel">
            <h3>moves</h3>
            {!history.length && <span className="dim">no moves yet</span>}
            <div className="moves">
              {pairMoves(history, startFen).map((r) => (
                <span key={r.no}>
                  <span className="movenum">{r.no}.{r.w ? '' : '..'}</span>
                  {r.w && <span className="move">{r.w.san} </span>}
                  {r.b && <span className="move">{r.b.san} </span>}
                </span>
              ))}
            </div>
          </div>

          <div className="panel">
            <h3>ask the coach {asking && <span className="dim">· thinking…</span>}</h3>
            <div className="thread">
              {!thread.length && (
                <div className="dim small">
                  Ask about the position on the board. The coach is given the engine's
                  candidate moves, material and loose pieces, and answers from those.
                </div>
              )}
              {thread.map((t, i) => (
                <div key={i} className="qa">
                  <div className="qa-q">
                    {t.moveNo ? <span className="dim">move {t.moveNo} · </span> : null}
                    {t.q}
                  </div>
                  {t.error
                    ? <div className="qa-a" style={{ color: '#c53030' }}>{t.error}</div>
                    : <div className="qa-a">{t.a}</div>}
                </div>
              ))}
            </div>
            <textarea rows={2} value={question} placeholder="ask about this position…"
                      onChange={(e) => setQuestion(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); askCoach() }
                      }} />
            <div className="row wrap">
              <button onClick={() => askCoach()} disabled={asking || !question.trim()}>
                {asking ? 'asking…' : 'ask'}
              </button>
              {thread.length > 0 && (
                <button onClick={() => setThread([])} disabled={asking}>clear</button>
              )}
            </div>
            <div className="row wrap">
              {SUGGESTED.map((s2) => (
                <button key={s2} className="chip-btn" disabled={asking}
                        onClick={() => askCoach(s2)}>{s2}</button>
              ))}
            </div>
          </div>

          <div className="panel">
            <h3>set up a position</h3>
            <div className="row">
              <input value={fenDraft} onChange={(e) => setFenDraft(e.target.value)}
                     placeholder="paste FEN" onKeyDown={(e) => e.key === 'Enter' && loadFen()} />
              <button onClick={loadFen}>load</button>
            </div>
            {fenError && <div className="dim" style={{ color: '#c53030' }}>{fenError}</div>}
            <div className="dim small">Current: {fen}</div>
          </div>
        </div>
      </div>
    </div>
  )
}
