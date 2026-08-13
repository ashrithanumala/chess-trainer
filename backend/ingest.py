"""Pull games from the chess.com public API into SQLite.

Usage:
    python ingest.py --user ashrithanumala --from 2026-06
    python ingest.py --user ashrithanumala --months 12 --class rapid,blitz
"""

import argparse
import io
import time
import urllib.request
import urllib.error
import json
from datetime import date

import chess.pgn

import db

API = "https://api.chess.com/pub"
UA = "chess-trainer/0.1 (personal analysis tool)"


def _get(url: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return {}


def archives(user: str) -> list[str]:
    return _get(f"{API}/player/{user.lower()}/games/archives").get("archives", [])


def _headers(pgn_text: str) -> dict:
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return {}
    ply = sum(1 for _ in game.mainline_moves())
    return {"hdr": dict(game.headers), "ply_count": ply}


def parse_game(raw: dict, user: str) -> dict | None:
    """Turn one chess.com API game object into a games-table row."""
    pgn_text = raw.get("pgn")
    if not pgn_text:
        return None
    meta = _headers(pgn_text)
    if not meta:
        return None
    hdr = meta["hdr"]

    white = raw["white"]["username"]
    black = raw["black"]["username"]
    ul = user.lower()
    my_color = "white" if white.lower() == ul else "black" if black.lower() == ul else None

    result = hdr.get("Result")
    my_result = None
    if my_color and result:
        if result == "1/2-1/2":
            my_result = "draw"
        elif (result == "1-0") == (my_color == "white"):
            my_result = "win"
        else:
            my_result = "loss"

    # chess.com encodes the outcome per side; pick the loser's reason for a readable label
    termination = hdr.get("Termination")

    return {
        "id": raw.get("uuid") or raw["url"],
        "url": raw.get("url"),
        "pgn": pgn_text,
        "white": white,
        "black": black,
        "white_elo": raw["white"].get("rating"),
        "black_elo": raw["black"].get("rating"),
        "result": result,
        "my_color": my_color,
        "my_result": my_result,
        "termination": termination,
        "time_class": raw.get("time_class"),
        "time_control": raw.get("time_control"),
        "eco": hdr.get("ECO"),
        "opening": _opening_name(hdr),
        "played_at": raw.get("end_time"),
        "ply_count": meta["ply_count"],
    }


def _opening_name(hdr: dict) -> str | None:
    """chess.com puts the opening in ECOUrl as a slug; Opening header is rare."""
    if hdr.get("Opening"):
        return hdr["Opening"]
    url = hdr.get("ECOUrl")
    if not url:
        return None
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ")


UPSERT = """
INSERT INTO games (id, url, pgn, white, black, white_elo, black_elo, result,
                   my_color, my_result, termination, time_class, time_control,
                   eco, opening, played_at, ply_count)
VALUES (:id, :url, :pgn, :white, :black, :white_elo, :black_elo, :result,
        :my_color, :my_result, :termination, :time_class, :time_control,
        :eco, :opening, :played_at, :ply_count)
ON CONFLICT(id) DO UPDATE SET
    pgn = excluded.pgn,
    eco = excluded.eco,
    opening = excluded.opening,
    ply_count = excluded.ply_count
"""


def ingest(user: str, since: str | None, months: int | None, classes: set[str] | None) -> dict:
    conn = db.init()
    urls = archives(user)
    if not urls:
        raise SystemExit(f"no archives for user '{user}' (typo? private profile?)")

    if since:
        y, m = (int(x) for x in since.split("-"))
        cutoff = y * 12 + m
        urls = [u for u in urls if _month_key(u) >= cutoff]
    elif months:
        today = date.today()
        cutoff = (today.year * 12 + today.month) - months + 1
        urls = [u for u in urls if _month_key(u) >= cutoff]

    stats = {"archives": len(urls), "seen": 0, "stored": 0, "skipped_class": 0, "unparsed": 0}
    for url in urls:
        payload = _get(url)
        rows = []
        for raw in payload.get("games", []):
            stats["seen"] += 1
            if classes and raw.get("time_class") not in classes:
                stats["skipped_class"] += 1
                continue
            row = parse_game(raw, user)
            if row is None:
                stats["unparsed"] += 1
                continue
            rows.append(row)
        if rows:
            conn.executemany(UPSERT, rows)
            conn.commit()
            stats["stored"] += len(rows)
        print(f"  {url.rsplit('/games/',1)[-1]}: +{len(rows)}")
    conn.close()
    return stats


def _month_key(archive_url: str) -> int:
    y, m = archive_url.rsplit("/", 2)[-2:]
    return int(y) * 12 + int(m)


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest chess.com games into SQLite")
    p.add_argument("--user", required=True)
    p.add_argument("--from", dest="since", help="earliest archive, YYYY-MM")
    p.add_argument("--months", type=int, help="pull the last N months instead")
    p.add_argument("--class", dest="classes",
                   help="comma list: rapid,blitz,bullet,daily (default all)")
    a = p.parse_args()

    classes = {c.strip() for c in a.classes.split(",")} if a.classes else None
    stats = ingest(a.user, a.since, a.months, classes)
    print(f"\narchives {stats['archives']} | seen {stats['seen']} | "
          f"stored {stats['stored']} | skipped(class) {stats['skipped_class']} | "
          f"unparsed {stats['unparsed']}")
    print(f"db: {db.DB_PATH}")


if __name__ == "__main__":
    main()
