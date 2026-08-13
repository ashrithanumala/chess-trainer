# Chess Trainer

Local chess analysis and training tool. Pulls your chess.com games, evaluates
every move with Stockfish, explains your mistakes, lets you branch off any
position and play it out against the engine, and keeps your notes and tags
searchable.

Everything runs on your machine. No account, no cloud, no rate limit.

## What is outsourced vs. custom

| Layer | What it is |
|---|---|
| Engine | Stockfish 18 (Homebrew, native arm64) |
| Board UI | [chessground](https://github.com/lichess-org/chessground), lichess's board component |
| Move rules | chess.js |
| Game data | chess.com public API |
| Storage | SQLite |
| **Custom** | game library, review walker, coach commentary, tags/notes/search, opening report |

## Setup

```bash
brew install stockfish
python3 -m pip install chess fastapi "uvicorn[standard]"
cd frontend && npm install && npm run build

# local coach commentary (optional but on by default)
brew install ollama && brew services start ollama
ollama pull qwen3:14b
```

The model is only ever asked to put Stockfish's findings into words — it never
evaluates a position or picks a move. Move facts (piece, squares, captures,
castling side, what the move leaves undefended) are computed with python-chess
and handed over as data, because small models invent that detail otherwise.

## Use

```bash
# 1. pull games
cd backend
python3 ingest.py --user YOURNAME --from 2026-06        # or --months 12
python3 ingest.py --user YOURNAME --months 6 --class rapid,blitz

# 2. analyze (this is the slow part; resumable, safe to re-run)
python3 analyze.py --class rapid --depth 16 --workers 3

# 3. generate commentary + auto tags
python3 coach.py

# 4. run the app
cd .. && ./run.sh          # http://localhost:8787
```

Dev mode with hot reload: `cd frontend && npm run dev` (proxies /api to 8787).

## In the app

- **library** — filter by speed, result, color, tag, or free text over your notes
  and the coach's commentary. Click a game to review it.
- **review** — arrow keys walk the game. Move list is colored by move quality;
  the eval graph is clickable. The green arrow is the engine's preferred move.
  - **coach mode** pauses before each of your blunders and mistakes and asks you
    to find something better. Your attempt is evaluated live, not just compared
    to a stored answer.
  - **play vs engine** — make any move from any position to branch off. With this
    on, Stockfish answers and you keep playing. Turn it off for free exploration
    of both sides. "back to game line" returns you to the real game.
  - **tags and notes** — notes attach to the current move. Auto tags come from the
    engine pass (`threw-away-win`, `missed-mate`, `time-trouble`, ...); your own
    tags sit alongside them.
  - **explain in words** — optional LLM commentary layered on the engine facts.
    Needs `ANTHROPIC_API_KEY` in the server environment; without it the button
    reports that and nothing else changes.
- **play** — free-play board. Pick a side, or paste a FEN to start from any
  position. Two independent toggles: **engine plays the other side** (best
  moves, or handicapped down to ~800) and **coaching** (green best-move arrow,
  live eval, top 3 lines). Turn the engine off to move both sides yourself.
  Take back rewinds the pair so you land on your own move.
  **Ask the coach** — a question box tied to the position on the board. Before
  the model sees anything, the server computes the facts: whose move it is, the
  moves played, Stockfish's top three candidate moves with evaluations and
  continuations, material, and which pieces are loose. The model answers from
  those and is told the candidates have not been played. Follow-up questions
  keep the thread, so "why not the other one?" works.
- **sync games** (header button) — pulls new games from chess.com and starts the
  engine pass in the background, with live progress. Re-pulling a month you
  already have updates those rows instead of duplicating; analysis skips games
  already done at that depth. Replaces the CLI loop for day-to-day use.
- **fix my openings** — positions from your own games where you bleed the most
  eval, worst first, with replacement moves. Red arrow is what you play, green
  is what to play instead. Move a piece to drill the fix against the engine.
  With `LICHESS_TOKEN` set it also shows what your rating band plays and how
  those moves score; without it, engine advice only.
  While drilling you choose who answers: **I pick the reply** lists the
  opponent's options from three sources — moves your actual opponents played
  from that position (with how you scored against each), moves common at your
  rating, and the engine's best (the hardest test) — or switch to **engine
  replies** to have Stockfish answer automatically.
- **openings** — per-ECO results joined to engine-measured error rate in the
  first 10 moves. Score% is raw outcome; opening ACPL and opening errors say
  whether the opening itself is the problem or the game was lost later.
- **search** — full text over notes and commentary.
- **stats** — move quality by phase, ACPL by speed.

## How moves are judged

One engine evaluation per position, reused as both "eval after this move" and
"eval before the next", so a game costs plies+1 evaluations.

Centipawns are converted to win probability with the Lichess curve, and the
classification is the drop in win probability caused by the move:

| wp loss | label |
|---|---|
| >= 20 | blunder |
| >= 10 | mistake |
| >= 5 | inaccuracy |
| played == engine's move | best |
| only legal move | forced |

Evals are clamped to +-1000cp before measuring loss, so being down a queen vs.
down two queens is not counted as a fresh blunder.

## Optional tokens

| Env var | Enables | Without it |
|---|---|---|
| (none) | "explain in words" runs **locally by default** on `qwen3:14b` via Ollama — free, private, ~8s per comment | n/a |
| `COACH_BACKEND=anthropic` + `ANTHROPIC_API_KEY` | use the hosted model instead; better prose, costs per call | stays local |
| `COACH_MODEL`, `COACH_BASE_URL`, `COACH_API_STYLE` | point at another local model or server (LM Studio, llama.cpp, vLLM) | defaults to Ollama on :11434 |
| `COACH_NO_THINK=0` | let a reasoning model think before answering (slower, no quality gain here) | thinking off |
| `COACH_KEEP_ALIVE` | how long the model stays in RAM after the last question (default `5m`, then it unloads itself) | 5 minutes |
| `COACH_AUTOSTART=0` | require Ollama to already be running instead of starting it on demand | started automatically on first question |

Nothing LLM-related runs until you press "explain in words". The first question
starts the Ollama daemon (~1s) and loads the model (~10s more); later questions
answer in ~8s. After `COACH_KEEP_ALIVE` the model unloads and RAM goes back to
zero. To have Ollama always resident instead, `brew services start ollama`.
| `LICHESS_TOKEN` | human win rates in **fix my openings** (the public explorer now requires auth; make a no-scope token at lichess.org/account/oauth/token) | recommendations come from the engine only |

## What runs when

Nothing heavy is resident between sessions.

| Process | Starts | Stops |
|---|---|---|
| Stockfish (interactive) | first eval, engine move, or coach question | `ENGINE_IDLE_TIMEOUT` seconds after the last request (default 120) |
| Stockfish (batch) | only when you start an analysis job | when the job finishes, or via **stop analysis** |
| Ollama daemon + model | first coach question | model after `COACH_KEEP_ALIVE` (default 5m) |

The header shows a dot: grey = engine off, green = engine resident (with an idle
countdown and a **stop** button), amber = a batch analysis is running. `GET
/api/engine/status` reports the same thing; `POST /api/engine/stop` releases it
immediately.

Pulling games does **not** start an engine pass unless you tick the box.

## Cost

Depth 16 on Apple silicon runs ~0.28s per position per worker. With 3 workers:

| set | games | wall clock |
|---|---|---|
| rapid (June+July) | 49 | ~7 min |
| blitz (June+July) | 282 | ~40 min |
| bullet (June+July) | 423 | ~50 min |

`analyze.py` skips games already analyzed at that depth, so it is resumable.
Raise `--depth` for stronger judgment at proportional cost.

## Layout

```
backend/
  db.py           schema + connection
  ingest.py       chess.com API -> games table
  analyze.py      batch Stockfish -> positions table
  analyze_one.py  single game, used by the API's analyze button
  coach.py        engine facts -> sentences + auto tags
  llm.py          optional LLM commentary
  server.py       FastAPI: library, review, notes/tags/search, live engine
frontend/
  src/Board.jsx   chessground wrapper
  src/Review.jsx  review walker, coach mode, branch play
  src/GameList.jsx, src/Reports.jsx, src/App.jsx
  smoke.test.mjs  headless render test against a running server
data/chess.db     everything
```
