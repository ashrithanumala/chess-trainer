"""Batch Stockfish pass: evaluate every ply of every game into the positions table.

One engine analysis per position, reused as both "eval after my move" and
"eval before opponent's move", so a game costs plies+1 evaluations, not 2x.

Usage:
    python analyze.py --class rapid --depth 16
    python analyze.py --limit 20 --workers 3 --redo
"""

import argparse
import io
import math
import multiprocessing as mp
import os
import queue as queue_mod
import re
import time

import chess
import chess.engine
import chess.pgn

import db

ENGINE = os.environ.get("STOCKFISH_PATH", "/opt/homebrew/bin/stockfish")

# Evals beyond this are noise for blame purposes: down a queen vs down two queens
# is the same practical mistake, so clamp before measuring loss.
CLAMP = 1000
MATE_CP = 10000

CLK_RE = re.compile(r"\[%clk\s+([0-9:.]+)\]")

_engine = None


def _shutdown_engine() -> None:
    """SimpleEngine holds a non-daemon thread, so the process cannot exit until
    the engine is closed. Workers must call this explicitly before returning."""
    global _engine
    if _engine is not None:
        try:
            _engine.quit()
        except Exception:
            pass
        _engine = None


def _get_engine(threads: int, hash_mb: int):
    global _engine
    if _engine is None:
        _engine = chess.engine.SimpleEngine.popen_uci(ENGINE)
        _engine.configure({"Threads": threads, "Hash": hash_mb})
    return _engine


def win_prob(cp: int) -> float:
    """Centipawns -> win probability 0..100, side-to-move POV (Lichess curve)."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def classify(wp_loss: float, played_is_best: bool, forced: bool) -> str:
    if forced:
        return "forced"
    if played_is_best:
        return "best"
    if wp_loss >= 20:
        return "blunder"
    if wp_loss >= 10:
        return "mistake"
    if wp_loss >= 5:
        return "inaccuracy"
    return "good"


def phase_of(board: chess.Board, ply: int) -> str:
    """Crude but stable: material-based endgame test, ply-based opening test."""
    non_pawn = 0
    for pt, val in ((chess.QUEEN, 9), (chess.ROOK, 5), (chess.BISHOP, 3), (chess.KNIGHT, 3)):
        non_pawn += val * len(board.pieces(pt, chess.WHITE))
        non_pawn += val * len(board.pieces(pt, chess.BLACK))
    if non_pawn <= 13:
        return "endgame"
    if ply < 20:
        return "opening"
    return "middlegame"


def _score_to_cp(score: chess.engine.PovScore, pov: bool) -> tuple[int, int | None]:
    """Return (centipawns, mate_distance_or_None) from `pov` side's perspective."""
    rel = score.pov(pov)
    if rel.is_mate():
        m = rel.mate()
        return (MATE_CP if m > 0 else -MATE_CP), m
    return rel.score(), None


def analyze_game(game_id: str, pgn_text: str, depth: int, threads: int, hash_mb: int) -> dict:
    """Evaluate one game. Returns rows for the positions table plus per-side ACPL."""
    engine = _get_engine(threads, hash_mb)
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return {"game_id": game_id, "rows": [], "acpl": (None, None)}

    board = game.board()
    nodes = list(game.mainline())
    if not nodes:
        return {"game_id": game_id, "rows": [], "acpl": (None, None)}

    limit = chess.engine.Limit(depth=depth)

    def evaluate(b: chess.Board):
        """(cp, mate, best_uci, best_san, pv_san) from side-to-move POV."""
        if b.is_checkmate():
            return -MATE_CP, 0, None, None, None
        if b.is_game_over():
            return 0, None, None, None, None
        info = engine.analyse(b, limit)
        cp, mate = _score_to_cp(info["score"], b.turn)
        pv = info.get("pv") or []
        best_uci = pv[0].uci() if pv else None
        best_san = b.san(pv[0]) if pv else None
        pv_san = b.variation_san(pv[:8]) if pv else None
        return cp, mate, best_uci, best_san, pv_san

    # Evaluate every position once: index i is the position before ply i.
    evals = []
    probe = game.board()
    evals.append(evaluate(probe))
    for node in nodes:
        probe.push(node.move)
        evals.append(evaluate(probe))

    rows = []
    losses = {"white": [], "black": []}
    for i, node in enumerate(nodes):
        mover = board.turn
        side = "white" if mover == chess.WHITE else "black"
        cp_before, mate_before, best_uci, best_san, pv_san = evals[i]
        cp_next, mate_next, _, _, _ = evals[i + 1]
        # evals[i+1] is from the opponent's POV; negate to keep one perspective.
        cp_after = -cp_next
        mate_after = -mate_next if mate_next is not None else None

        cb = max(-CLAMP, min(CLAMP, cp_before))
        ca = max(-CLAMP, min(CLAMP, cp_after))
        cp_loss = max(0, cb - ca)
        wp_before, wp_after = win_prob(cb), win_prob(ca)
        wp_loss = max(0.0, wp_before - wp_after)

        forced = board.legal_moves.count() == 1
        played_is_best = best_uci is not None and node.move.uci() == best_uci
        cls = classify(wp_loss, played_is_best, forced)

        clk = CLK_RE.search(node.comment or "")
        rows.append({
            "game_id": game_id,
            "ply": i,
            "fen": board.fen(),
            "side": side,
            "move_no": board.fullmove_number,
            "san": board.san(node.move),
            "uci": node.move.uci(),
            "best_san": best_san,
            "best_uci": best_uci,
            "pv": pv_san,
            "cp_before": cp_before,
            "cp_after": cp_after,
            "cp_loss": cp_loss,
            "wp_before": round(wp_before, 2),
            "wp_after": round(wp_after, 2),
            "wp_loss": round(wp_loss, 2),
            "mate_before": mate_before,
            "mate_after": mate_after,
            "class": cls,
            "phase": phase_of(board, i),
            "clock": clk.group(1) if clk else None,
        })
        if not forced:
            losses[side].append(cp_loss)
        board.push(node.move)

    acpl = tuple(
        round(sum(v) / len(v)) if v else None
        for v in (losses["white"], losses["black"])
    )
    return {"game_id": game_id, "rows": rows, "acpl": acpl, "depth": depth}


INSERT = """
INSERT OR REPLACE INTO positions
(game_id, ply, fen, side, move_no, san, uci, best_san, best_uci, pv,
 cp_before, cp_after, cp_loss, wp_before, wp_after, wp_loss,
 mate_before, mate_after, class, phase, clock)
VALUES
(:game_id, :ply, :fen, :side, :move_no, :san, :uci, :best_san, :best_uci, :pv,
 :cp_before, :cp_after, :cp_loss, :wp_before, :wp_after, :wp_loss,
 :mate_before, :mate_after, :class, :phase, :clock)
"""


def pending(conn, classes, limit, redo, depth):
    q = "SELECT id, pgn FROM games WHERE ply_count > 0"
    args = []
    if classes:
        q += f" AND time_class IN ({','.join('?' * len(classes))})"
        args += list(classes)
    if not redo:
        q += " AND (analyzed_at IS NULL OR analysis_depth < ?)"
        args.append(depth)
    q += " ORDER BY played_at DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, args).fetchall()


def _worker(task_q, result_q, depth: int, threads: int, hash_mb: int) -> None:
    """Pull games off the queue until the sentinel, then close the engine.

    The engine must be closed inside the worker: its reader thread is non-daemon,
    so a worker that skips this never exits and the parent waits forever on join.
    """
    try:
        while True:
            item = task_q.get()
            if item is None:
                break
            gid, pgn_text = item
            try:
                result_q.put(analyze_game(gid, pgn_text, depth, threads, hash_mb))
            except Exception as e:
                result_q.put({"game_id": gid, "rows": [], "acpl": (None, None),
                              "error": f"{type(e).__name__}: {e}"})
    finally:
        _shutdown_engine()
        result_q.put(None)


def run_batch(conn, todo, depth: int, workers: int, threads: int, hash_mb: int) -> int:
    ctx = mp.get_context("spawn")
    task_q, result_q = ctx.Queue(), ctx.Queue()
    for row in todo:
        task_q.put((row["id"], row["pgn"]))
    for _ in range(workers):
        task_q.put(None)

    procs = [
        ctx.Process(target=_worker, args=(task_q, result_q, depth, threads, hash_mb),
                    daemon=True)
        for _ in range(workers)
    ]
    for pr in procs:
        pr.start()

    started, done, live, failed = time.time(), 0, workers, 0
    while live:
        try:
            res = result_q.get(timeout=600)
        except queue_mod.Empty:
            print("  timed out waiting on workers; aborting")
            break
        if res is None:
            live -= 1
            continue
        gid = res["game_id"]
        if res.get("error"):
            failed += 1
            print(f"  FAIL {gid}: {res['error']}")
            continue
        if res["rows"]:
            conn.executemany(INSERT, res["rows"])
            conn.execute(
                "UPDATE games SET analyzed_at=?, analysis_depth=?, acpl_white=?, acpl_black=? "
                "WHERE id=?",
                (int(time.time()), depth, res["acpl"][0], res["acpl"][1], gid),
            )
            conn.commit()
        done += 1
        if done % 5 == 0 or done == len(todo):
            el = time.time() - started
            eta = (len(todo) - done) * el / done if done else 0
            print(f"  {done}/{len(todo)}  {el/60:.1f}m elapsed  ~{eta/60:.1f}m left", flush=True)

    for pr in procs:
        pr.join(timeout=30)
        if pr.is_alive():
            pr.terminate()
    if failed:
        print(f"{failed} game(s) failed")
    return done


def main() -> None:
    p = argparse.ArgumentParser(description="Stockfish batch analysis")
    p.add_argument("--class", dest="classes", help="comma list of time classes")
    p.add_argument("--depth", type=int, default=16)
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--threads", type=int, default=2, help="engine threads per worker")
    p.add_argument("--hash", type=int, default=256, help="engine hash MB per worker")
    p.add_argument("--redo", action="store_true", help="re-analyze already done games")
    a = p.parse_args()

    classes = [c.strip() for c in a.classes.split(",")] if a.classes else None
    conn = db.init()
    todo = pending(conn, classes, a.limit, a.redo, a.depth)
    if not todo:
        print("nothing to analyze")
        return

    total_plies = conn.execute(
        f"SELECT COALESCE(SUM(ply_count),0) FROM games WHERE id IN ({','.join('?' * len(todo))})",
        [r["id"] for r in todo],
    ).fetchone()[0]
    print(f"{len(todo)} games / ~{total_plies} plies @ depth {a.depth}, "
          f"{a.workers} workers x {a.threads} threads")

    started = time.time()
    done = run_batch(conn, todo, a.depth, a.workers, a.threads, a.hash)
    print(f"analyzed {done} games in {(time.time()-started)/60:.1f}m")
    conn.close()


if __name__ == "__main__":
    main()
