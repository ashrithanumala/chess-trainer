import { useEffect, useRef, useState } from 'react'
import { api } from './api'

const CLASSES = ['rapid', 'blitz', 'bullet', 'daily']

// localStorage is absent in private browsing and in the headless test env.
const readStored = (k) => {
  try { return globalThis.localStorage?.getItem(k) || '' } catch { return '' }
}
const writeStored = (k, v) => {
  try { globalThis.localStorage?.setItem(k, v) } catch { /* not persisted */ }
}

/** Header control: pull new games from chess.com and kick off the engine pass. */
export default function Sync({ onDone }) {
  const [open, setOpen] = useState(false)
  const [user, setUser] = useState(readStored('cc_user'))
  const [months, setMonths] = useState(2)
  const [classes, setClasses] = useState(['rapid'])
  // Off by default: pulling games should not silently start a CPU-heavy batch.
  const [analyze, setAnalyze] = useState(false)
  const [status, setStatus] = useState(null)
  const [job, setJob] = useState(null)
  const [busy, setBusy] = useState(false)
  const timer = useRef(null)

  // Poll while a background analysis is running, then stop.
  useEffect(() => {
    const tick = async () => {
      try {
        const j = await api.jobs()
        setJob(j)
        if (!j.running && timer.current) {
          clearInterval(timer.current)
          timer.current = null
          onDone?.()
        }
      } catch { /* server restarting */ }
    }
    tick()
    return () => { if (timer.current) clearInterval(timer.current) }
  }, [])

  const startPolling = () => {
    if (timer.current) clearInterval(timer.current)
    timer.current = setInterval(async () => {
      const j = await api.jobs()
      setJob(j)
      if (!j.running) {
        clearInterval(timer.current)
        timer.current = null
        onDone?.()
      }
    }, 4000)
  }

  const run = async () => {
    if (!user.trim()) { setStatus('enter your chess.com username'); return }
    writeStored('cc_user', user.trim())
    setBusy(true)
    setStatus('pulling games…')
    try {
      const r = await api.sync({ user: user.trim(), months: Number(months), classes, analyze })
      const s = r.ingest
      setStatus(`${s.stored} games stored from ${s.archives} month(s)` +
                (r.analysis_started ? ' · analyzing in background' : ''))
      onDone?.()
      if (r.analysis_started) startPolling()
    } catch (e) {
      setStatus('failed: ' + e.message)
    } finally { setBusy(false) }
  }

  const toggle = (c) =>
    setClasses((cs) => (cs.includes(c) ? cs.filter((x) => x !== c) : [...cs, c]))

  return (
    <div className="sync">
      <button onClick={() => setOpen((o) => !o)}>
        {job?.running ? 'analyzing…' : 'sync games'}
      </button>

      {job?.running && <span className="dim job-inline">{job.tail?.split('\n').pop()}</span>}

      {open && (
        <div className="sync-panel">
          <div className="row">
            <input value={user} onChange={(e) => setUser(e.target.value)}
                   placeholder="chess.com username" />
            <select value={months} onChange={(e) => setMonths(e.target.value)}>
              <option value={1}>this month</option>
              <option value={2}>last 2 months</option>
              <option value={6}>last 6 months</option>
              <option value={12}>last 12 months</option>
            </select>
          </div>
          <div className="row wrap">
            {CLASSES.map((c) => (
              <label key={c} className="toggle">
                <input type="checkbox" checked={classes.includes(c)} onChange={() => toggle(c)} />
                {c}
              </label>
            ))}
          </div>
          <label className="toggle">
            <input type="checkbox" checked={analyze} onChange={(e) => setAnalyze(e.target.checked)} />
            run engine analysis on new games
          </label>
          <div className="row">
            <button onClick={run} disabled={busy || job?.running}>
              {busy ? 'working…' : 'pull now'}
            </button>
            {job?.running && (
              <button onClick={async () => { await api.stopJob(); setJob(await api.jobs()) }}>
                stop analysis
              </button>
            )}
          </div>
          {status && <div className="dim">{status}</div>}
          {job?.running && <pre className="joblog">{job.tail}</pre>}
          <div className="dim small">
            Re-pulling a month you already have updates those games instead of duplicating
            them. Analysis skips games already done at this depth.
          </div>
        </div>
      )}
    </div>
  )
}
