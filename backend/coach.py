"""Turn engine numbers into sentences, and engine patterns into searchable tags.

Tier 1: deterministic templates built only from facts Stockfish already gave us.
No model, no network, no hallucination. Tier 2 (LLM) layers on top in llm.py.
"""

import time

import chess

import db

PIECE_NAME = {
    chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
    chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king",
}
PIECE_VALUE = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100,
}

CLASS_LABEL = {
    "blunder": "Blunder", "mistake": "Mistake", "inaccuracy": "Inaccuracy",
    "good": "Fine", "best": "Best move", "forced": "Forced", "book": "Book",
}


def pawns(cp: int | None) -> str:
    if cp is None:
        return "?"
    return f"{cp/100:+.1f}"


def hanging_pieces(board: chess.Board, owner: chess.Color) -> list[tuple[str, str]]:
    """Pieces of `owner` that can be won by the side to move.

    Cheap static test, not a full SEE: a piece counts as hanging if it is attacked
    and either undefended or attacked by something worth less than it.
    """
    out = []
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.color != owner or piece.piece_type == chess.KING:
            continue
        attackers = board.attackers(not owner, sq)
        if not attackers:
            continue
        defenders = board.attackers(owner, sq)
        val = PIECE_VALUE[piece.piece_type]
        cheapest = min(
            PIECE_VALUE[board.piece_at(a).piece_type] for a in attackers
        )
        if not defenders or cheapest < val:
            out.append((PIECE_NAME[piece.piece_type], chess.square_name(sq)))
    return out


def comment(row, nxt=None) -> str:
    """One line of commentary for a single played move."""
    board = chess.Board(row["fen"])
    mover = board.turn
    san, best = row["san"], row["best_san"]
    cls = row["class"]
    parts = []

    # Headline: what kind of move was this, and what did it cost.
    if cls in ("blunder", "mistake", "inaccuracy"):
        parts.append(f"{CLASS_LABEL[cls]}. {san} "
                     f"({pawns(row['cp_before'])} -> {pawns(row['cp_after'])}).")
    elif cls == "best":
        parts.append(f"{san} is the engine's top choice ({pawns(row['cp_after'])}).")
    elif cls == "forced":
        parts.append(f"{san} is forced - no alternative.")
    else:
        parts.append(f"{san} is fine ({pawns(row['cp_after'])}).")

    # Mate swings dominate everything else, so report them before material.
    if row["mate_before"] is not None and row["mate_before"] > 0 and \
            (row["mate_after"] is None or row["mate_after"] <= 0):
        parts.append(f"Missed forced mate in {row['mate_before']}"
                     + (f" starting with {best}." if best else "."))
    if row["mate_after"] is not None and row["mate_after"] < 0:
        parts.append(f"Now allows mate in {abs(row['mate_after'])}.")

    # What the move physically dropped.
    after = board.copy()
    after.push(chess.Move.from_uci(row["uci"]))
    loose = hanging_pieces(after, mover)
    if loose and cls in ("blunder", "mistake"):
        names = ", ".join(f"{n} on {sq}" for n, sq in loose[:2])
        refutation = nxt["san"] if nxt else None
        best_reply = nxt["best_san"] if nxt else None
        tail = ""
        if best_reply:
            tail = f" {best_reply} takes it."
            if refutation and refutation != best_reply:
                tail += f" (Played {refutation} instead.)"
        parts.append(f"Leaves the {names} loose.{tail}")

    # The alternative, with a concrete continuation.
    if best and best != san and cls != "best":
        line = row["pv"] or ""
        parts.append(f"Better: {best}." + (f" Line: {line}" if line else ""))

    if row["phase"] == "opening" and cls in ("blunder", "mistake"):
        parts.append("This happened inside the opening - worth adding to your prep.")

    return " ".join(parts)


# ---------------------------------------------------------------- auto tags

def auto_tags(conn, game_id: str) -> list[str]:
    """Derive searchable labels for a game from its analyzed positions."""
    g = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    if g is None or g["my_color"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM positions WHERE game_id=? ORDER BY ply", (game_id,)
    ).fetchall()
    if not rows:
        return []

    me = g["my_color"]
    mine = [r for r in rows if r["side"] == me]
    tags = set()

    blunders = [r for r in mine if r["class"] == "blunder"]
    mistakes = [r for r in mine if r["class"] == "mistake"]
    if blunders:
        tags.add("blunder")
    if len(blunders) >= 3:
        tags.add("multi-blunder")
    if not blunders and not mistakes:
        tags.add("clean-game")

    for r in blunders + mistakes:
        if r["phase"] == "opening":
            tags.add("opening-error")
        elif r["phase"] == "endgame":
            tags.add("endgame-error")
        else:
            tags.add("middlegame-error")

    if any(r["mate_before"] is not None and r["mate_before"] > 0 and
           (r["mate_after"] is None or r["mate_after"] <= 0) for r in mine):
        tags.add("missed-mate")
    if any(r["mate_after"] is not None and r["mate_after"] < 0 for r in mine):
        tags.add("allowed-mate")

    # Did a winning or losing position get thrown away?
    evals = [r["cp_after"] if r["side"] == me else -r["cp_after"] for r in rows]
    peak, trough = max(evals), min(evals)
    if g["my_result"] == "loss" and peak >= 300:
        tags.add("threw-away-win")
    if g["my_result"] == "win" and trough <= -300:
        tags.add("saved-lost-game")
    if g["my_result"] == "draw" and peak >= 300:
        tags.add("drew-won-position")

    # Clock pressure: any bad move made under 20 seconds.
    for r in blunders + mistakes:
        if r["clock"] and _clock_seconds(r["clock"]) is not None and \
                _clock_seconds(r["clock"]) < 20:
            tags.add("time-trouble")
            break

    term = (g["termination"] or "").lower()
    if "time" in term:
        tags.add("flagged" if g["my_result"] == "loss" else "won-on-time")
    if "resignation" in term and g["my_result"] == "loss":
        tags.add("resigned")

    acpl = g["acpl_white"] if me == "white" else g["acpl_black"]
    if acpl is not None:
        if acpl <= 25:
            tags.add("well-played")
        elif acpl >= 100:
            tags.add("messy")

    if g["ply_count"] and g["ply_count"] <= 40:
        tags.add("short-game")
    if rows[-1]["phase"] == "endgame":
        tags.add("reached-endgame")

    return sorted(tags)


def _clock_seconds(clk: str) -> float | None:
    try:
        parts = [float(x) for x in clk.split(":")]
    except ValueError:
        return None
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


# ---------------------------------------------------------------- persistence

def annotate_game(conn, game_id: str, only_bad: bool = True) -> int:
    """Write coach commentary + auto tags for one analyzed game."""
    rows = conn.execute(
        "SELECT * FROM positions WHERE game_id=? ORDER BY ply", (game_id,)
    ).fetchall()
    if not rows:
        return 0
    now = int(time.time())

    keep = {"blunder", "mistake", "inaccuracy"} if only_bad else set(CLASS_LABEL)
    written = 0
    for i, r in enumerate(rows):
        if r["class"] not in keep:
            continue
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        body = comment(r, nxt)
        conn.execute(
            "INSERT OR REPLACE INTO annotations (game_id, ply, source, body, created_at) "
            "VALUES (?,?,?,?,?)",
            (game_id, r["ply"], "coach", body, now),
        )
        conn.execute("DELETE FROM search_fts WHERE game_id=? AND ply=? AND kind='annotation'",
                     (game_id, r["ply"]))
        conn.execute(
            "INSERT INTO search_fts (game_id, ply, kind, body) VALUES (?,?,'annotation',?)",
            (game_id, r["ply"], body),
        )
        written += 1

    conn.execute("DELETE FROM tags WHERE game_id=? AND auto=1", (game_id,))
    for t in auto_tags(conn, game_id):
        conn.execute("INSERT OR IGNORE INTO tags (game_id, tag, auto) VALUES (?,?,1)",
                     (game_id, t))
    conn.commit()
    return written


def annotate_all(conn, only_bad: bool = True) -> dict:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM games WHERE analyzed_at IS NOT NULL ORDER BY played_at DESC")]
    total = 0
    for gid in ids:
        total += annotate_game(conn, gid, only_bad)
    return {"games": len(ids), "annotations": total}


if __name__ == "__main__":
    conn = db.init()
    stats = annotate_all(conn)
    print(f"annotated {stats['games']} games, {stats['annotations']} comments")
    print("top tags:")
    for tag, n in conn.execute(
            "SELECT tag, COUNT(*) c FROM tags GROUP BY tag ORDER BY c DESC LIMIT 15"):
        print(f"  {tag:20} {n}")
