"""FastAPI backend: game library, review data, notes/tags/search, live engine.

Run:  uvicorn server:app --port 8787 --reload
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import chess
import chess.engine
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import coach
import db

ENGINE_PATH = os.environ.get("STOCKFISH_PATH", "/opt/homebrew/bin/stockfish")
ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = ROOT / "frontend" / "dist"

app = FastAPI(title="chess-trainer")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def conn():
    return db.init()


# ------------------------------------------------------------------ engine

ENGINE_IDLE_TIMEOUT = int(os.environ.get("ENGINE_IDLE_TIMEOUT", "120"))


class _Engine:
    """One Stockfish shared by all requests, started on demand and released when idle.

    UCI is a single-conversation protocol, so concurrent analyse calls on one
    process would interleave and corrupt each other - hence the lock and the
    single instance. A watchdog quits the process after ENGINE_IDLE_TIMEOUT
    seconds without a request, so nothing lingers between sessions; the next
    call transparently starts a fresh one.
    """

    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._watchdog = None

    def _ensure(self):
        if self._engine is None:
            self._engine = chess.engine.SimpleEngine.popen_uci(ENGINE_PATH)
            self._engine.configure({"Threads": 2, "Hash": 256})
            self._start_watchdog()
        self._last_used = time.time()
        return self._engine

    def _start_watchdog(self):
        if self._watchdog is not None and self._watchdog.is_alive():
            return

        def loop():
            while True:
                time.sleep(5)
                with self._lock:
                    if self._engine is None:
                        return
                    if time.time() - self._last_used < ENGINE_IDLE_TIMEOUT:
                        continue
                    self._quit_locked()
                    return

        self._watchdog = threading.Thread(target=loop, daemon=True)
        self._watchdog.start()

    def _quit_locked(self):
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None

    def analyse(self, board: chess.Board, depth: int, multipv: int = 1):
        with self._lock:
            eng = self._ensure()
            try:
                out = eng.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
            except chess.engine.EngineTerminatedError:
                self._engine = None                     # died; retry once with a fresh one
                eng = self._ensure()
                out = eng.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
            self._last_used = time.time()
            return out

    def play(self, board: chess.Board, skill: int, movetime_ms: int):
        with self._lock:
            eng = self._ensure()
            eng.configure({"Skill Level": max(0, min(20, skill))})
            out = eng.play(board, chess.engine.Limit(time=movetime_ms / 1000))
            self._last_used = time.time()
            return out

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._engine is not None,
                "idle_seconds": round(time.time() - self._last_used, 1) if self._engine else None,
                "idle_timeout": ENGINE_IDLE_TIMEOUT,
            }

    def close(self):
        with self._lock:
            self._quit_locked()


engine = _Engine()


@app.on_event("shutdown")
def _shutdown():
    engine.close()


@app.get("/api/engine/status")
def engine_status():
    """Whether Stockfish is currently resident, and how close it is to release."""
    return {**engine.status(), "job_running": _job_running(), "job": JOB.get("name")}


@app.post("/api/engine/stop")
def engine_stop():
    """Release the engine now instead of waiting for the idle timeout."""
    engine.close()
    return {"ok": True}


def _score_fields(info, board: chess.Board) -> dict:
    rel = info["score"].pov(board.turn)
    pv = info.get("pv") or []
    return {
        "cp": None if rel.is_mate() else rel.score(),
        "mate": rel.mate() if rel.is_mate() else None,
        "best_uci": pv[0].uci() if pv else None,
        "best_san": board.san(pv[0]) if pv else None,
        "pv": board.variation_san(pv[:10]) if pv else None,
        "depth": info.get("depth"),
    }


class EvalReq(BaseModel):
    fen: str
    depth: int = 16
    multipv: int = 1


@app.post("/api/engine/eval")
def engine_eval(req: EvalReq):
    try:
        board = chess.Board(req.fen)
    except ValueError:
        raise HTTPException(400, "bad fen")
    if board.is_game_over():
        return {"game_over": True, "result": board.result(), "lines": []}
    info = engine.analyse(board, req.depth, req.multipv)
    infos = info if isinstance(info, list) else [info]
    return {"game_over": False, "lines": [_score_fields(i, board) for i in infos]}


class PlayReq(BaseModel):
    fen: str
    skill: int = 8           # 0-20, Stockfish Skill Level
    movetime_ms: int = 300


@app.post("/api/engine/play")
def engine_play(req: PlayReq):
    try:
        board = chess.Board(req.fen)
    except ValueError:
        raise HTTPException(400, "bad fen")
    if board.is_game_over():
        return {"game_over": True, "result": board.result(), "move": None}
    result = engine.play(board, req.skill, req.movetime_ms)
    if result.move is None:
        return {"game_over": True, "result": board.result(), "move": None}
    san = board.san(result.move)
    board.push(result.move)
    return {
        "game_over": board.is_game_over(),
        "result": board.result() if board.is_game_over() else None,
        "move": {"uci": result.move.uci(), "san": san},
        "fen": board.fen(),
    }


# ------------------------------------------------------------------ library

@app.get("/api/games")
def list_games(
    time_class: str | None = Query(None, alias="class"),
    result: str | None = None,
    color: str | None = None,
    eco: str | None = None,
    tag: str | None = None,
    analyzed: bool | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    c = conn()
    where, args = ["1=1"], []
    if time_class:
        parts = [x.strip() for x in time_class.split(",")]
        where.append(f"g.time_class IN ({','.join('?' * len(parts))})")
        args += parts
    if result:
        where.append("g.my_result = ?")
        args.append(result)
    if color:
        where.append("g.my_color = ?")
        args.append(color)
    if eco:
        where.append("g.eco = ?")
        args.append(eco)
    if analyzed is not None:
        where.append("g.analyzed_at IS NOT NULL" if analyzed else "g.analyzed_at IS NULL")
    if tag:
        for t in [x.strip() for x in tag.split(",") if x.strip()]:
            where.append("EXISTS (SELECT 1 FROM tags t WHERE t.game_id=g.id AND t.tag=?)")
            args.append(t)
    if q:
        where.append("(g.opening LIKE ? OR g.white LIKE ? OR g.black LIKE ? OR "
                     "EXISTS (SELECT 1 FROM search_fts s WHERE s.game_id=g.id "
                     "AND search_fts MATCH ?))")
        args += [f"%{q}%", f"%{q}%", f"%{q}%", q]

    sql = f"""
      SELECT g.*,
        (SELECT COUNT(*) FROM positions p WHERE p.game_id=g.id AND p.class='blunder'
           AND p.side=g.my_color) AS my_blunders,
        (SELECT COUNT(*) FROM positions p WHERE p.game_id=g.id AND p.class='mistake'
           AND p.side=g.my_color) AS my_mistakes,
        (SELECT GROUP_CONCAT(t.tag) FROM tags t WHERE t.game_id=g.id) AS tag_list,
        (SELECT COUNT(*) FROM notes n WHERE n.game_id=g.id) AS note_count
      FROM games g
      WHERE {' AND '.join(where)}
      ORDER BY g.played_at DESC
      LIMIT ? OFFSET ?
    """
    rows = [dict(r) for r in c.execute(sql, args + [limit, offset])]
    for r in rows:
        r.pop("pgn", None)
        r["tags"] = sorted(set((r.pop("tag_list") or "").split(","))) if r.get("tag_list") else []
    total = c.execute(f"SELECT COUNT(*) FROM games g WHERE {' AND '.join(where)}",
                      args).fetchone()[0]
    c.close()
    return {"total": total, "games": rows}


@app.get("/api/games/{game_id}")
def get_game(game_id: str):
    c = conn()
    g = c.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    if g is None:
        raise HTTPException(404, "no such game")
    positions = [dict(r) for r in c.execute(
        "SELECT * FROM positions WHERE game_id=? ORDER BY ply", (game_id,))]
    annotations = {}
    for r in c.execute("SELECT * FROM annotations WHERE game_id=?", (game_id,)):
        annotations.setdefault(r["ply"], []).append({"source": r["source"], "body": r["body"]})
    notes = [dict(r) for r in c.execute(
        "SELECT * FROM notes WHERE game_id=? ORDER BY COALESCE(ply,-1), id", (game_id,))]
    tags = [dict(r) for r in c.execute(
        "SELECT tag, auto FROM tags WHERE game_id=? ORDER BY auto, tag", (game_id,))]
    c.close()
    return {"game": dict(g), "positions": positions,
            "annotations": annotations, "notes": notes, "tags": tags}


class AnalyzeReq(BaseModel):
    depth: int = 16


@app.post("/api/games/{game_id}/analyze")
def analyze_one(game_id: str, req: AnalyzeReq):
    """Analyze a single game synchronously in a subprocess, then annotate it."""
    c = conn()
    g = c.execute("SELECT id FROM games WHERE id=?", (game_id,)).fetchone()
    if g is None:
        raise HTTPException(404, "no such game")
    c.close()
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "analyze_one.py"), game_id, str(req.depth)],
        capture_output=True, text=True, cwd=str(Path(__file__).parent),
    )
    if proc.returncode != 0:
        raise HTTPException(500, f"analysis failed: {proc.stderr[-500:]}")
    c = conn()
    written = coach.annotate_game(c, game_id)
    c.close()
    return {"ok": True, "annotations": written}


@app.post("/api/games/{game_id}/explain")
def llm_explain(game_id: str):
    """Tier 2 commentary. No-ops with a clear message when no API key is set."""
    import llm
    c = conn()
    try:
        written = llm.annotate_game(c, game_id)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        c.close()
    return {"ok": True, "written": written}


# ------------------------------------------------------------------ notes/tags

class NoteReq(BaseModel):
    body: str
    ply: int | None = None


@app.post("/api/games/{game_id}/notes")
def add_note(game_id: str, req: NoteReq):
    c = conn()
    now = int(time.time())
    cur = c.execute(
        "INSERT INTO notes (game_id, ply, body, created_at, updated_at) VALUES (?,?,?,?,?)",
        (game_id, req.ply, req.body, now, now),
    )
    c.execute("INSERT INTO search_fts (game_id, ply, kind, body) VALUES (?,?,'note',?)",
              (game_id, req.ply, req.body))
    c.commit()
    note = dict(c.execute("SELECT * FROM notes WHERE id=?", (cur.lastrowid,)).fetchone())
    c.close()
    return note


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int):
    c = conn()
    row = c.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such note")
    c.execute("DELETE FROM notes WHERE id=?", (note_id,))
    c.execute("DELETE FROM search_fts WHERE game_id=? AND kind='note' AND body=?",
              (row["game_id"], row["body"]))
    c.commit()
    c.close()
    return {"ok": True}


class TagReq(BaseModel):
    tag: str


@app.post("/api/games/{game_id}/tags")
def add_tag(game_id: str, req: TagReq):
    tag = req.tag.strip().lower().replace(" ", "-")
    if not tag:
        raise HTTPException(400, "empty tag")
    c = conn()
    c.execute("INSERT OR IGNORE INTO tags (game_id, tag, auto) VALUES (?,?,0)", (game_id, tag))
    c.commit()
    c.close()
    return {"ok": True, "tag": tag}


@app.delete("/api/games/{game_id}/tags/{tag}")
def remove_tag(game_id: str, tag: str):
    c = conn()
    c.execute("DELETE FROM tags WHERE game_id=? AND tag=? AND auto=0", (game_id, tag))
    c.commit()
    c.close()
    return {"ok": True}


@app.get("/api/tags")
def all_tags():
    c = conn()
    rows = [{"tag": r[0], "auto": r[1], "count": r[2]} for r in c.execute(
        "SELECT tag, auto, COUNT(*) FROM tags GROUP BY tag, auto ORDER BY COUNT(*) DESC")]
    c.close()
    return rows


@app.get("/api/search")
def search(q: str, limit: int = 50):
    """Full text over notes and coach commentary, newest game first."""
    c = conn()
    rows = [dict(r) for r in c.execute("""
        SELECT s.game_id, s.ply, s.kind, snippet(search_fts, 3, '<b>', '</b>', '...', 12) AS excerpt,
               g.white, g.black, g.opening, g.my_result, g.played_at, g.time_class
        FROM search_fts s JOIN games g ON g.id = s.game_id
        WHERE search_fts MATCH ?
        ORDER BY g.played_at DESC LIMIT ?
    """, (q, limit))]
    c.close()
    return rows


# ------------------------------------------------------------------ sync

SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)
"""

# One background analysis at a time; the engine already saturates the CPU.
JOB = {"name": None, "proc": None, "log": None, "started": None}


def _setting(c, key, value=None):
    c.execute(SETTINGS_DDL)
    if value is not None:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
        c.commit()
        return value
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


class SyncReq(BaseModel):
    user: str | None = None
    months: int = 2
    classes: list[str] = ["rapid"]
    analyze: bool = True
    depth: int = 16


@app.post("/api/sync")
def sync(req: SyncReq):
    """Pull new games from chess.com, then optionally start an engine pass.

    Ingest is quick and runs inline; analysis is slow so it goes to a background
    process whose progress is polled via /api/jobs.
    """
    import ingest

    c = conn()
    user = req.user or _setting(c, "user")
    if not user:
        c.close()
        raise HTTPException(400, "no chess.com username set")
    _setting(c, "user", user)
    c.close()

    try:
        stats = ingest.ingest(user, since=None, months=req.months,
                              classes=set(req.classes) if req.classes else None)
    except SystemExit as e:
        raise HTTPException(400, str(e))

    started = False
    if req.analyze and not _job_running():
        c = conn()
        q = "SELECT COUNT(*) FROM games WHERE analyzed_at IS NULL AND ply_count > 0"
        args = []
        if req.classes:
            q += f" AND time_class IN ({','.join('?' * len(req.classes))})"
            args = req.classes
        pending_n = c.execute(q, args).fetchone()[0]
        c.close()
        if pending_n:
            started = _start_analysis(req.classes, req.depth)

    return {"ok": True, "user": user, "ingest": stats, "analysis_started": started}


def _job_running() -> bool:
    return JOB["proc"] is not None and JOB["proc"].poll() is None


def _start_analysis(classes: list[str], depth: int) -> bool:
    if _job_running():
        return False
    log_path = ROOT / "data" / "job.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-u", "analyze.py", "--depth", str(depth), "--workers", "3"]
    if classes:
        cmd += ["--class", ",".join(classes)]
    log = open(log_path, "w")
    JOB.update({
        "name": f"analyze {','.join(classes) or 'all'} @ depth {depth}",
        "proc": subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                 cwd=str(Path(__file__).parent)),
        "log": log_path,
        "started": int(time.time()),
    })
    return True


class AnalyzeBatchReq(BaseModel):
    classes: list[str] = ["rapid"]
    depth: int = 16


@app.post("/api/analyze/batch")
def analyze_batch(req: AnalyzeBatchReq):
    if _job_running():
        raise HTTPException(409, "a job is already running")
    if not _start_analysis(req.classes, req.depth):
        raise HTTPException(500, "could not start job")
    return {"ok": True}


@app.get("/api/jobs")
def jobs():
    running = _job_running()
    tail = ""
    if JOB["log"] and Path(JOB["log"]).exists():
        lines = Path(JOB["log"]).read_text(errors="replace").strip().splitlines()
        tail = "\n".join(lines[-3:])
    # When a run finishes, fold its results into commentary and tags.
    if JOB["proc"] is not None and not running and not JOB.get("annotated"):
        c = conn()
        coach.annotate_all(c)
        c.close()
        JOB["annotated"] = True
    if running:
        JOB["annotated"] = False
    return {
        "running": running,
        "name": JOB["name"],
        "started": JOB["started"],
        "tail": tail,
    }


@app.post("/api/jobs/stop")
def stop_job():
    if _job_running():
        JOB["proc"].terminate()
    return {"ok": True}


# ------------------------------------------------------------------ repertoire

@app.get("/api/repertoire")
def repertoire_report(
    color: str = "white",
    time_class: str | None = Query(None, alias="class"),
    min_games: int = 3,
    depth: int = 18,
):
    """Where your opening leaks, and concrete replacements."""
    import repertoire as rep

    c = conn()
    classes = [x.strip() for x in time_class.split(",")] if time_class else None
    leaks = rep.find_leaks(c, color, classes, min_games=min_games)
    out = [rep.enrich(c, lk, engine=engine, depth=depth) for lk in leaks]
    c.close()
    return {
        "color": color,
        "explorer_available": not any(o.get("explorer_error") == "explorer_unauthorized"
                                      for o in out),
        "leaks": out,
    }


@app.get("/api/replies")
def opponent_replies(fen: str, my_color: str = "white", depth: int = 14):
    """Common opponent answers to the position on the board."""
    import repertoire as rep

    c = conn()
    try:
        out = rep.replies(c, fen, my_color, engine=engine, depth=depth)
    finally:
        c.close()
    return out


class AskReq(BaseModel):
    fen: str
    question: str
    moves: list[str] = []                 # SAN played so far, for context
    history: list[dict] = []              # prior [{q, a}] turns in this thread
    depth: int = 16


def position_facts(board: chess.Board, moves: list[str], depth: int) -> dict:
    """Everything the coach is allowed to reason from, computed not guessed."""
    import coach

    names = {chess.PAWN: "pawns", chess.KNIGHT: "knights", chess.BISHOP: "bishops",
             chess.ROOK: "rooks", chess.QUEEN: "queens"}
    material = {}
    for color, label in ((chess.WHITE, "white"), (chess.BLACK, "black")):
        material[label] = {n: len(board.pieces(pt, color)) for pt, n in names.items()}

    lines = []
    if not board.is_game_over():
        info = engine.analyse(board, depth, multipv=3)
        for i in (info if isinstance(info, list) else [info]):
            pv = i.get("pv") or []
            if not pv:
                continue
            rel = i["score"].pov(chess.WHITE)      # one perspective throughout
            lines.append({
                "candidate_move": board.san(pv[0]),
                "eval_white_pawns": None if rel.is_mate() else round(rel.score() / 100, 2),
                "mate_in": rel.mate() if rel.is_mate() else None,
                "continuation": board.variation_san(pv[:8]),
            })

    return {
        "fen": board.fen(),
        "side_to_move": "white" if board.turn == chess.WHITE else "black",
        "move_number": board.fullmove_number,
        "moves_played": " ".join(moves) if moves else "(position was set up directly)",
        "in_check": board.is_check(),
        "castling_rights": board.castling_xfen().split(" ")[-1] if board.castling_rights else "none",
        "material": material,
        "candidate_moves_not_yet_played": lines,
        "note": ("candidate_moves_not_yet_played are suggestions for the side to "
                 "move. None of them is on the board. Only moves_played have "
                 "actually happened."),
        "loose_white_pieces": [f"{n} on {sq}" for n, sq in coach.hanging_pieces(board, chess.WHITE)],
        "loose_black_pieces": [f"{n} on {sq}" for n, sq in coach.hanging_pieces(board, chess.BLACK)],
        "game_over": board.is_game_over(),
    }


@app.post("/api/ask")
def ask_coach(req: AskReq):
    """Ask the coach about any position. Engine facts first, model second."""
    import llm

    if not req.question.strip():
        raise HTTPException(400, "empty question")
    try:
        board = chess.Board(req.fen)
    except ValueError:
        raise HTTPException(400, "bad fen")

    facts = position_facts(board, req.moves, req.depth)
    try:
        answer = llm.ask(req.question.strip(), facts, req.history)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"coach unavailable: {type(e).__name__}: {e}")
    if not answer:
        raise HTTPException(502, "coach returned nothing")
    return {"answer": answer, "facts": facts}


# ------------------------------------------------------------------ reports

@app.get("/api/stats")
def stats():
    c = conn()
    out = {}
    out["totals"] = dict(c.execute("""
        SELECT COUNT(*) games,
               SUM(analyzed_at IS NOT NULL) analyzed,
               SUM(my_result='win') wins,
               SUM(my_result='loss') losses,
               SUM(my_result='draw') draws
        FROM games""").fetchone())
    out["by_class"] = [dict(r) for r in c.execute("""
        SELECT time_class,
               COUNT(*) n,
               SUM(my_result='win') wins,
               ROUND(AVG(CASE WHEN my_color='white' THEN acpl_white ELSE acpl_black END),1) acpl
        FROM games GROUP BY time_class ORDER BY n DESC""")]
    out["move_classes"] = [dict(r) for r in c.execute("""
        SELECT p.class, p.phase, COUNT(*) n
        FROM positions p JOIN games g ON g.id=p.game_id
        WHERE p.side = g.my_color
        GROUP BY p.class, p.phase""")]
    c.close()
    return out


@app.get("/api/openings")
def openings(min_games: int = 2, time_class: str | None = Query(None, alias="class")):
    """Per-opening results joined to engine-measured error rate.

    Win rate alone is noise at low volume; opening ACPL and opening-phase blunder
    counts say whether the line itself is the problem.
    """
    c = conn()
    where, args = ["g.my_color IS NOT NULL"], []
    if time_class:
        parts = [x.strip() for x in time_class.split(",")]
        where.append(f"g.time_class IN ({','.join('?' * len(parts))})")
        args += parts
    # Game-level and position-level aggregates must be computed separately:
    # joining positions before SUM() multiplies each game by its ply count.
    rows = [dict(r) for r in c.execute(f"""
        WITH sel AS (
            SELECT id, eco, opening, my_color, my_result, analyzed_at
            FROM games g WHERE {' AND '.join(where)}
        ),
        res AS (
            SELECT eco, my_color,
                   MAX(opening) opening,
                   COUNT(*) games,
                   SUM(my_result='win')  wins,
                   SUM(my_result='loss') losses,
                   SUM(my_result='draw') draws,
                   SUM(analyzed_at IS NOT NULL) analyzed
            FROM sel GROUP BY eco, my_color
        ),
        pos AS (
            SELECT s.eco, s.my_color,
                   ROUND(AVG(CASE WHEN p.phase='opening' THEN p.cp_loss END), 1) opening_acpl,
                   SUM(CASE WHEN p.phase='opening'
                             AND p.class IN ('blunder','mistake') THEN 1 ELSE 0 END) opening_errors
            FROM sel s JOIN positions p ON p.game_id = s.id AND p.side = s.my_color
            GROUP BY s.eco, s.my_color
        )
        SELECT res.eco, res.opening, res.my_color, res.games, res.wins, res.losses,
               res.draws, res.analyzed, pos.opening_acpl, pos.opening_errors
        FROM res LEFT JOIN pos ON pos.eco IS res.eco AND pos.my_color = res.my_color
        WHERE res.games >= ?
        ORDER BY res.games DESC
    """, args + [min_games])]
    c.close()
    for r in rows:
        r["score"] = round((r["wins"] + 0.5 * r["draws"]) / r["games"] * 100, 1)
    return rows


# ------------------------------------------------------------------ static

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIST / "index.html")
