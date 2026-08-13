"""SQLite schema and connection helpers for the chess trainer."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "chess.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS games (
    id            TEXT PRIMARY KEY,      -- chess.com game url, stable unique id
    url           TEXT,
    pgn           TEXT NOT NULL,
    white         TEXT NOT NULL,
    black         TEXT NOT NULL,
    white_elo     INTEGER,
    black_elo     INTEGER,
    result        TEXT,                  -- '1-0', '0-1', '1/2-1/2'
    my_color      TEXT,                  -- 'white' | 'black' | NULL if user not in game
    my_result     TEXT,                  -- 'win' | 'loss' | 'draw'
    termination   TEXT,
    time_class    TEXT,                  -- rapid | blitz | bullet | daily
    time_control  TEXT,
    eco           TEXT,
    opening       TEXT,
    played_at     INTEGER,               -- unix seconds
    ply_count     INTEGER,
    analyzed_at   INTEGER,               -- NULL until engine pass completes
    analysis_depth INTEGER,
    acpl_white    INTEGER,
    acpl_black    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_games_played  ON games(played_at DESC);
CREATE INDEX IF NOT EXISTS idx_games_class   ON games(time_class);
CREATE INDEX IF NOT EXISTS idx_games_eco     ON games(eco);
CREATE INDEX IF NOT EXISTS idx_games_result  ON games(my_result);

CREATE TABLE IF NOT EXISTS positions (
    game_id     TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply         INTEGER NOT NULL,        -- 0-based index of the move played from this position
    fen         TEXT NOT NULL,           -- position BEFORE the move
    side        TEXT NOT NULL,           -- 'white' | 'black' = side to move
    move_no     INTEGER NOT NULL,        -- 1-based full move number
    san         TEXT NOT NULL,           -- move actually played
    uci         TEXT NOT NULL,
    best_san    TEXT,                    -- engine's preferred move
    best_uci    TEXT,
    pv          TEXT,                    -- engine principal variation, space separated SAN
    cp_before   INTEGER,                 -- eval before move, side-to-move POV, centipawns
    cp_after    INTEGER,                 -- eval after move, same POV (negated from opponent)
    cp_loss     INTEGER,                 -- max(0, cp_before - cp_after)
    wp_before   REAL,                    -- win probability 0-100, side-to-move POV
    wp_after    REAL,
    wp_loss     REAL,
    mate_before INTEGER,                 -- signed mate distance if forced mate, else NULL
    mate_after  INTEGER,
    class       TEXT,                    -- best|good|inaccuracy|mistake|blunder|forced|book
    phase       TEXT,                    -- opening|middlegame|endgame
    clock       TEXT,                    -- remaining clock after move, from PGN comment
    PRIMARY KEY (game_id, ply)
);

CREATE INDEX IF NOT EXISTS idx_pos_class ON positions(class);
CREATE INDEX IF NOT EXISTS idx_pos_fen   ON positions(fen);
CREATE INDEX IF NOT EXISTS idx_pos_game  ON positions(game_id, ply);

-- User-authored notes. ply NULL means the note is about the whole game.
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id    TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply        INTEGER,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_game ON notes(game_id);

-- Machine-generated commentary: engine facts turned into prose, or LLM output.
CREATE TABLE IF NOT EXISTS annotations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id    TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply        INTEGER NOT NULL,
    source     TEXT NOT NULL,            -- 'coach' (templates) | 'llm'
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(game_id, ply, source)
);

CREATE INDEX IF NOT EXISTS idx_annot_game ON annotations(game_id, ply);

CREATE TABLE IF NOT EXISTS tags (
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    auto    INTEGER NOT NULL DEFAULT 0,  -- 1 = generated from engine data, 0 = user
    ply     INTEGER,                     -- optional anchor to a specific move
    PRIMARY KEY (game_id, tag, auto)
);

CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

-- Full text search over notes and annotations so 'search my games' is one query.
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    game_id UNINDEXED,
    ply     UNINDEXED,
    kind    UNINDEXED,                   -- 'note' | 'annotation'
    body,
    tokenize = 'porter unicode61'
);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


if __name__ == "__main__":
    conn = init()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    )]
    print(f"{DB_PATH}")
    print("tables:", ", ".join(tables))
