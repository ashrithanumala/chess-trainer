"""Analyze a single game in its own process, so the API never holds an engine open.

Usage: python analyze_one.py <game_id> [depth]
"""

import sys
import time

import analyze
import db


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: analyze_one.py <game_id> [depth]", file=sys.stderr)
        return 2
    game_id = sys.argv[1]
    depth = int(sys.argv[2]) if len(sys.argv) > 2 else 16

    conn = db.init()
    row = conn.execute("SELECT id, pgn FROM games WHERE id=?", (game_id,)).fetchone()
    if row is None:
        print(f"no such game: {game_id}", file=sys.stderr)
        return 1

    try:
        res = analyze.analyze_game(row["id"], row["pgn"], depth, threads=4, hash_mb=512)
    finally:
        analyze._shutdown_engine()

    if res["rows"]:
        conn.executemany(analyze.INSERT, res["rows"])
        conn.execute(
            "UPDATE games SET analyzed_at=?, analysis_depth=?, acpl_white=?, acpl_black=? "
            "WHERE id=?",
            (int(time.time()), depth, res["acpl"][0], res["acpl"][1], game_id),
        )
        conn.commit()
    conn.close()
    print(f"{game_id}: {len(res['rows'])} plies @ depth {depth}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
