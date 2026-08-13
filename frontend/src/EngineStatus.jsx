import { useEffect, useState } from 'react'
import { api } from './api'

/** Header readout: is Stockfish resident right now, and why. */
export default function EngineStatus() {
  const [s, setS] = useState(null)

  useEffect(() => {
    let stop = false
    const tick = async () => {
      try {
        const r = await api.engineStatus()
        if (!stop) setS(r)
      } catch { /* server down */ }
    }
    tick()
    const t = setInterval(tick, 8000)
    return () => { stop = true; clearInterval(t) }
  }, [])

  if (!s) return null
  const busy = s.job_running
  const on = s.running || busy
  const label = busy ? `analyzing: ${s.job}`
    : s.running ? `engine on (idle ${Math.round(s.idle_seconds)}s of ${s.idle_timeout}s)`
    : 'engine off'

  return (
    <span className="engine-status" title={label}>
      <span className={'dot' + (busy ? ' busy' : on ? ' on' : '')} />
      <span className="dim">{busy ? 'analyzing' : s.running ? 'engine on' : 'engine off'}</span>
      {s.running && !busy && (
        <button className="chip-btn" onClick={async () => { await api.engineStop(); setS(await api.engineStatus()) }}>
          stop
        </button>
      )}
    </span>
  )
}
