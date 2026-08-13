"""Find where your opening actually leaks, and what to play instead.

Method:
  1. Merge the first N plies of your games into a position tree (transpositions
     collapse, since positions are keyed by FEN not by move order).
  2. A node is a "leak" if you have played it enough times and either bleed
     centipawns there or score badly.
  3. For each leak, ask the Lichess opening explorer what players in your rating
     band actually play from that exact position, and ask Stockfish what it
     thinks of the alternatives.

The explorer gives practical results (what wins for humans at 1000); the engine
gives objective truth. A good replacement move should look fine to both.
"""

import json
import os
import time
import urllib.parse
import urllib.request

import chess

import db

EXPLORER = "https://explorer.lichess.ovh/lichess"
UA = "chess-trainer/0.1 (personal analysis tool)"

# The public explorer started requiring auth. Without a token we fall back to
# engine-only recommendations, which still work, just without human win rates.
# Make one at https://lichess.org/account/oauth/token (no scopes needed).
TOKEN_ENV = "LICHESS_TOKEN"

ENGINE_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS engine_cache (
    fen        TEXT NOT NULL,
    depth      INTEGER NOT NULL,
    multipv    INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (fen, depth, multipv)
);
"""

CACHE_DDL = """
CREATE TABLE IF NOT EXISTS explorer_cache (
    fen        TEXT NOT NULL,
    params     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (fen, params)
);
"""


def _pos_key(fen: str) -> str:
    """FEN without move counters, so the same position from different move
    orders is one node."""
    return " ".join(fen.split(" ")[:4])


def explorer(conn, fen: str, ratings=(1000, 1200, 1400), speeds=("blitz", "rapid"),
             ttl_days: int = 30) -> dict:
    conn.execute(CACHE_DDL)
    params = urllib.parse.urlencode({
        "variant": "standard",
        "fen": fen,
        "speeds": ",".join(speeds),
        "ratings": ",".join(str(r) for r in ratings),
        "moves": 8,
        "topGames": 0,
        "recentGames": 0,
    })
    row = conn.execute("SELECT payload, fetched_at FROM explorer_cache WHERE fen=? AND params=?",
                       (fen, params)).fetchone()
    if row and time.time() - row["fetched_at"] < ttl_days * 86400:
        return json.loads(row["payload"])

    headers = {"User-Agent": UA}
    token = os.environ.get(TOKEN_ENV)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(f"{EXPLORER}?{params}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"error": "explorer_unauthorized", "moves": []}
        if e.code == 429:
            return {"error": "explorer_rate_limited", "moves": []}
        return {"error": f"HTTP {e.code}", "moves": []}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "moves": []}

    conn.execute(
        "INSERT OR REPLACE INTO explorer_cache (fen, params, payload, fetched_at) VALUES (?,?,?,?)",
        (fen, params, json.dumps(payload), int(time.time())))
    conn.commit()
    return payload


def build_tree(conn, color: str, classes: list[str] | None, max_ply: int = 16) -> dict:
    """Merge my games into {position_key: node} for the opening phase."""
    where = ["g.my_color = ?", "p.ply < ?", "p.side = g.my_color"]
    args: list = [color, max_ply]
    if classes:
        where.append(f"g.time_class IN ({','.join('?' * len(classes))})")
        args += classes

    rows = conn.execute(f"""
        SELECT p.fen, p.san, p.uci, p.ply, p.move_no, p.cp_loss, p.class,
               g.id game_id, g.my_result, g.eco, g.opening
        FROM positions p JOIN games g ON g.id = p.game_id
        WHERE {' AND '.join(where)}
        ORDER BY p.ply
    """, args).fetchall()

    nodes: dict[str, dict] = {}
    for r in rows:
        key = _pos_key(r["fen"])
        node = nodes.setdefault(key, {
            "fen": r["fen"], "ply": r["ply"], "move_no": r["move_no"],
            "games": set(), "moves": {}, "cp_loss": 0, "n_moves": 0,
            "wins": 0, "losses": 0, "draws": 0, "ecos": {},
        })
        if r["eco"]:
            node["ecos"][(r["eco"], r["opening"])] = node["ecos"].get((r["eco"], r["opening"]), 0) + 1
        node["games"].add(r["game_id"])
        node["cp_loss"] += r["cp_loss"] or 0
        node["n_moves"] += 1
        mv = node["moves"].setdefault(r["san"], {
            "san": r["san"], "uci": r["uci"], "n": 0, "cp_loss": 0,
            "wins": 0, "losses": 0, "draws": 0, "errors": 0,
        })
        mv["n"] += 1
        mv["cp_loss"] += r["cp_loss"] or 0
        if r["class"] in ("blunder", "mistake"):
            mv["errors"] += 1
        for bucket, target in (("win", "wins"), ("loss", "losses"), ("draw", "draws")):
            if r["my_result"] == bucket:
                mv[target] += 1
                node[target] += 1
    return nodes


def _score(w: int, l: int, d: int) -> float | None:
    n = w + l + d
    return round((w + 0.5 * d) / n * 100, 1) if n else None


def find_leaks(conn, color: str, classes: list[str] | None = None,
               min_games: int = 3, max_ply: int = 16, top: int = 8) -> list[dict]:
    """Positions in your own opening where you lose the most, worst first."""
    nodes = build_tree(conn, color, classes, max_ply)
    leaks = []
    for key, node in nodes.items():
        n = len(node["games"])
        if n < min_games:
            continue
        avg_loss = node["cp_loss"] / max(1, node["n_moves"])
        score = _score(node["wins"], node["losses"], node["draws"])
        if avg_loss < 40 and (score is None or score > 45):
            continue
        # Rank by how much is bled and how often the position comes up.
        severity = avg_loss * n + (50 - (score if score is not None else 50)) * n
        leaks.append({
            "key": key, "fen": node["fen"], "ply": node["ply"],
            "move_no": node["move_no"], "games": n,
            "avg_cp_loss": round(avg_loss, 1), "score": score,
            "severity": round(severity, 1),
            "my_eco": (max(node["ecos"], key=node["ecos"].get)[0] if node["ecos"] else None),
            "my_opening": (max(node["ecos"], key=node["ecos"].get)[1] if node["ecos"] else None),
            "my_moves": sorted(
                [{**m,
                  "avg_cp_loss": round(m["cp_loss"] / m["n"], 1),
                  "score": _score(m["wins"], m["losses"], m["draws"])}
                 for m in node["moves"].values()],
                key=lambda m: -m["n"]),
        })
    leaks.sort(key=lambda x: -x["severity"])
    return leaks[:top]


def enrich(conn, leak: dict, engine=None, depth: int = 18) -> dict:
    """Attach what your rating band plays here, plus the engine's opinion."""
    data = explorer(conn, leak["fen"])
    board = chess.Board(leak["fen"])
    mover_is_white = board.turn == chess.WHITE

    book = []
    for m in (data.get("moves") or [])[:6]:
        w, d, b = m.get("white", 0), m.get("draws", 0), m.get("black", 0)
        total = w + d + b
        if not total:
            continue
        mine = w if mover_is_white else b
        theirs = b if mover_is_white else w
        book.append({
            "san": m.get("san"),
            "uci": m.get("uci"),
            "games": total,
            "score": round((mine + 0.5 * d) / total * 100, 1),
            "popularity": round(total / max(1, sum(
                (x.get("white", 0) + x.get("draws", 0) + x.get("black", 0))
                for x in data.get("moves") or [])) * 100, 1),
            "avg_rating": m.get("averageRating"),
        })
        _ = theirs

    # Engine analysis of the same handful of positions is requested on every
    # page view, so cache it: same fen + depth always gives the same answer.
    conn.execute(ENGINE_CACHE_DDL)
    cached = conn.execute(
        "SELECT payload FROM engine_cache WHERE fen=? AND depth=? AND multipv=3",
        (leak["fen"], depth)).fetchone()

    engine_lines = []
    if cached:
        engine_lines = json.loads(cached["payload"])
    elif engine is not None:
        try:
            infos = engine.analyse(board, depth, multipv=3)
            infos = infos if isinstance(infos, list) else [infos]
            for i in infos:
                pv = i.get("pv") or []
                if not pv:
                    continue
                rel = i["score"].pov(board.turn)
                engine_lines.append({
                    "san": board.san(pv[0]),
                    "uci": pv[0].uci(),
                    "cp": None if rel.is_mate() else rel.score(),
                    "mate": rel.mate() if rel.is_mate() else None,
                    "line": board.variation_san(pv[:6]),
                })
        except Exception as e:
            engine_lines = [{"error": f"{type(e).__name__}: {e}"}]
        if engine_lines and not any("error" in e for e in engine_lines):
            conn.execute(
                "INSERT OR REPLACE INTO engine_cache (fen, depth, multipv, payload, fetched_at) "
                "VALUES (?,?,3,?,?)",
                (leak["fen"], depth, json.dumps(engine_lines), int(time.time())))
            conn.commit()

    my_main = leak["my_moves"][0]["san"] if leak["my_moves"] else None
    engine_sans = {e.get("san") for e in engine_lines}

    if book:
        # Best case: a move must both be played by humans at your level and
        # survive the engine's opinion.
        recs = [
            {**b, "why": "played at your rating and engine-approved"
                  if b["san"] in engine_sans else "scores well at your rating"}
            for b in book
            if b["san"] != my_main and (b["san"] in engine_sans or b["score"] >= 50)
        ][:3]
    else:
        # No explorer access: engine-only. Still actionable, just without the
        # human win-rate evidence.
        recs = [
            {"san": e["san"], "uci": e["uci"], "cp": e.get("cp"), "line": e.get("line"),
             "games": None, "score": None, "popularity": None, "avg_rating": None,
             "why": "engine's preference (no human data available)"}
            for e in engine_lines if e.get("san") and e.get("san") != my_main
        ][:3]

    return {
        **leak,
        "opening": (data.get("opening") or {}).get("name") or leak.get("my_opening"),
        "eco": (data.get("opening") or {}).get("eco") or leak.get("my_eco"),
        "book": book,
        "engine": engine_lines,
        "recommendations": recs,
        "explorer_error": data.get("error"),
    }


def replies(conn, fen: str, my_color: str, engine=None, depth: int = 14) -> dict:
    """What the opponent actually plays from this position.

    Three independent sources, because each answers a different question:
      mine     - what THESE opponents did to you (small sample, maximally relevant)
      book     - what your whole rating band does (large sample, needs a token)
      engine   - what is objectively best (always available, not what you will face)
    """
    board = chess.Board(fen)
    opp_color = "black" if my_color == "white" else "white"
    if (board.turn == chess.WHITE) != (opp_color == "white"):
        return {"mine": [], "book": [], "engine": [], "error": "not the opponent's move"}

    prefix = _pos_key(fen)
    rows = conn.execute("""
        SELECT p.san, p.uci, g.my_result
        FROM positions p JOIN games g ON g.id = p.game_id
        WHERE p.fen LIKE ? AND p.side = ? AND g.my_color = ?
    """, (prefix + " %", opp_color, my_color)).fetchall()

    agg: dict[str, dict] = {}
    for r in rows:
        m = agg.setdefault(r["san"], {"san": r["san"], "uci": r["uci"],
                                      "n": 0, "wins": 0, "losses": 0, "draws": 0})
        m["n"] += 1
        if r["my_result"] == "win":
            m["wins"] += 1
        elif r["my_result"] == "loss":
            m["losses"] += 1
        elif r["my_result"] == "draw":
            m["draws"] += 1
    mine = sorted(agg.values(), key=lambda m: -m["n"])
    for m in mine:
        m["my_score"] = _score(m["wins"], m["losses"], m["draws"])

    data = explorer(conn, fen)
    book = []
    total_all = sum((x.get("white", 0) + x.get("draws", 0) + x.get("black", 0))
                    for x in (data.get("moves") or [])) or 1
    for m in (data.get("moves") or [])[:6]:
        w, d, b = m.get("white", 0), m.get("draws", 0), m.get("black", 0)
        total = w + d + b
        if not total:
            continue
        mine_pts = w if my_color == "white" else b
        book.append({
            "san": m.get("san"), "uci": m.get("uci"), "games": total,
            "popularity": round(total / total_all * 100, 1),
            "my_score": round((mine_pts + 0.5 * d) / total * 100, 1),
        })

    engine_moves = []
    conn.execute(ENGINE_CACHE_DDL)
    cached = conn.execute(
        "SELECT payload FROM engine_cache WHERE fen=? AND depth=? AND multipv=3",
        (fen, depth)).fetchone()
    if cached:
        engine_moves = json.loads(cached["payload"])
    elif engine is not None:
        try:
            infos = engine.analyse(board, depth, multipv=3)
            infos = infos if isinstance(infos, list) else [infos]
            for i in infos:
                pv = i.get("pv") or []
                if not pv:
                    continue
                rel = i["score"].pov(board.turn)
                engine_moves.append({
                    "san": board.san(pv[0]), "uci": pv[0].uci(),
                    "cp": None if rel.is_mate() else rel.score(),
                    "mate": rel.mate() if rel.is_mate() else None,
                })
            if engine_moves:
                conn.execute(
                    "INSERT OR REPLACE INTO engine_cache (fen, depth, multipv, payload, fetched_at)"
                    " VALUES (?,?,3,?,?)",
                    (fen, depth, json.dumps(engine_moves), int(time.time())))
                conn.commit()
        except Exception as e:
            engine_moves = [{"error": f"{type(e).__name__}: {e}"}]

    return {"mine": mine, "book": book, "engine": engine_moves,
            "explorer_error": data.get("error")}


if __name__ == "__main__":
    import sys
    color = sys.argv[1] if len(sys.argv) > 1 else "white"
    conn = db.init()
    leaks = find_leaks(conn, color, classes=None)
    print(f"{len(leaks)} leak positions as {color}\n")
    for lk in leaks:
        full = enrich(conn, lk)
        print(f"move {full['move_no']} · {full['games']} games · "
              f"acpl {full['avg_cp_loss']} · score {full['score']}")
        print(f"  {full['eco'] or ''} {full['opening'] or ''}")
        print(f"  you play: " + ", ".join(
            f"{m['san']} x{m['n']} (acpl {m['avg_cp_loss']}, score {m['score']})"
            for m in full["my_moves"][:3]))
        if full["book"]:
            print(f"  1000-1400 play: " + ", ".join(
                f"{b['san']} {b['popularity']}% scoring {b['score']}" for b in full["book"][:4]))
        if full["recommendations"]:
            print(f"  -> try: " + ", ".join(r["san"] for r in full["recommendations"]))
        print()
