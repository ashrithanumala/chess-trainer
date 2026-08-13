import { useEffect, useRef } from 'react'
import { Chessground } from 'chessground'

/**
 * Thin wrapper over lichess's chessground. All board rendering, dragging and
 * animation is outsourced; we only feed it state.
 */
export default function Board({
  fen,
  orientation = 'white',
  lastMove,
  dests,
  turnColor,
  movableColor,
  onMove,
  shapes = [],
  viewOnly = false,
  check = false,
}) {
  const ref = useRef(null)
  const cg = useRef(null)
  const onMoveRef = useRef(onMove)
  onMoveRef.current = onMove

  useEffect(() => {
    if (!ref.current) return
    cg.current = Chessground(ref.current, {
      fen,
      orientation,
      viewOnly,
      animation: { enabled: true, duration: 180 },
      highlight: { lastMove: true, check: true },
      movable: {
        free: false,
        showDests: true,
        events: { after: (from, to) => onMoveRef.current?.(from, to) },
      },
      drawable: { enabled: true, visible: true },
    })
    return () => cg.current?.destroy()
  }, [])

  useEffect(() => {
    if (!cg.current) return
    cg.current.set({
      fen,
      orientation,
      viewOnly,
      check,
      lastMove,
      turnColor,
      movable: { free: false, color: movableColor, dests, showDests: true },
      drawable: { autoShapes: shapes },
    })
  }, [fen, orientation, lastMove, dests, turnColor, movableColor, viewOnly, check, shapes])

  return <div className="board-wrap" ref={ref} />
}
