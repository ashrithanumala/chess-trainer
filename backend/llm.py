"""Tier 2 commentary: natural-language explanation on top of engine facts.

Optional, and backend-agnostic. Two modes:

  COACH_BACKEND=anthropic   (default)  needs ANTHROPIC_API_KEY
  COACH_BACKEND=local                  any OpenAI-compatible server, e.g. Ollama
                                       (COACH_BASE_URL=http://localhost:11434/v1)

The model never judges the position: Stockfish already decided what the mistake
was. The model only turns given facts into an explanation, which is why a
modest local model is enough here even though local models play chess badly.
Output is cached per position, so each line is generated at most once.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

import chess

import coach
import db

BACKEND = os.environ.get("COACH_BACKEND", "anthropic").lower()
MODEL = os.environ.get("COACH_MODEL") or (
    "qwen3:14b" if BACKEND == "local" else "claude-sonnet-5")
BASE_URL = os.environ.get("COACH_BASE_URL", "http://localhost:11434/v1").rstrip("/")
NO_THINK = os.environ.get("COACH_NO_THINK", "1") not in ("0", "false", "no")
# How long Ollama keeps the model in RAM after the last request. It unloads
# itself after this, so nothing occupies memory between questions.
KEEP_ALIVE = os.environ.get("COACH_KEEP_ALIVE", "5m")
AUTOSTART = os.environ.get("COACH_AUTOSTART", "1") not in ("0", "false", "no")
API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = (
    "You are a chess coach for a ~1000 rated club player. "
    "You are given the exact engine facts for one move. Explain in 2-3 sentences "
    "why the move was bad and what the better move accomplishes, in plain language: "
    "name the tactic or plan, do not restate the numbers, do not invent variations "
    "beyond the principal variation you are given. "
    "Never contradict the engine evaluation you were given, and never claim a "
    "move is good or bad other than as stated in the facts. "
    "Use only the squares, pieces and castling side given in the facts - do not "
    "infer board geometry yourself. Reply with prose only."
)


def _move_facts(board: "chess.Board", san: str | None) -> dict | None:
    """Describe a move in terms the model would otherwise have to infer.

    Small models confidently get this wrong — calling O-O-O "kingside", naming
    the wrong piece, inventing captures. Stating it removes the guesswork.
    """
    if not san:
        return None
    try:
        mv = board.parse_san(san)
    except ValueError:
        return {"san": san}
    piece = board.piece_at(mv.from_square)
    captured = board.piece_at(mv.to_square)
    facts = {
        "san": san,
        "piece": coach.PIECE_NAME.get(piece.piece_type) if piece else None,
        "from": chess.square_name(mv.from_square),
        "to": chess.square_name(mv.to_square),
        "is_capture": board.is_capture(mv),
        "captures": coach.PIECE_NAME.get(captured.piece_type) if captured else None,
        "gives_check": board.gives_check(mv),
    }
    if board.is_castling(mv):
        facts["castling"] = "kingside" if board.is_kingside_castling(mv) else "queenside"
    after = board.copy()
    after.push(mv)
    loose = coach.hanging_pieces(after, piece.color if piece else board.turn)
    if loose:
        facts["leaves_undefended"] = [f"{n} on {sq}" for n, sq in loose[:3]]
    return facts


def _prompt(row) -> str:
    board = chess.Board(row["fen"])
    return json.dumps({
        "position_fen": row["fen"],
        "side_to_move": row["side"],
        "move_played": _move_facts(board, row["san"]),
        "engine_best_move": _move_facts(board, row["best_san"]),
        "principal_variation": row["pv"],
        "eval_before_pawns": None if row["cp_before"] is None else row["cp_before"] / 100,
        "eval_after_pawns": None if row["cp_after"] is None else row["cp_after"] / 100,
        "mate_before": row["mate_before"],
        "mate_after": row["mate_after"],
        "classification": row["class"],
        "phase": row["phase"],
    }, indent=1)


def _strip_thinking(text: str) -> str:
    """Local reasoning models emit <think>...</think> before the answer."""
    while "<think>" in text and "</think>" in text:
        head, rest = text.split("<think>", 1)
        text = head + rest.split("</think>", 1)[1]
    return text.strip()


def _explain_anthropic(row, api_key: str, timeout: int) -> str:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 300,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": _prompt(row)}],
    }).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.load(r)
    return "".join(b.get("text", "") for b in payload.get("content", [])).strip()


def _post(url: str, body: dict, timeout: int) -> dict:
    headers = {"content-type": "application/json"}
    key = os.environ.get("COACH_API_KEY")
    if key:
        headers["authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _ollama_host() -> str:
    return BASE_URL[:-3] if BASE_URL.endswith("/v1") else BASE_URL


def _ollama_up(host: str, timeout: float = 1.0) -> bool:
    try:
        urllib.request.urlopen(f"{host}/api/version", timeout=timeout)
        return True
    except Exception:
        return False


def ensure_ollama(wait_s: int = 30) -> bool:
    """Start the Ollama daemon on demand, so nothing runs until you ask.

    The daemon is a thin listener; the model itself is loaded on first request
    and unloaded again after KEEP_ALIVE, so idle cost is zero either way.
    """
    host = _ollama_host()
    if _ollama_up(host):
        return True
    if not AUTOSTART:
        return False
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise RuntimeError("ollama is not installed (brew install ollama)")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if _ollama_up(host):
            return True
        time.sleep(0.5)
    raise RuntimeError("ollama did not start in time")


def _explain_ollama(row, timeout: int) -> str:
    """Ollama's native chat API, which has a real switch for thinking.

    Prompt-level tricks like "/no_think" are unreliable: the model still emits a
    reasoning block, which eats the token budget and leaves an empty answer.
    """
    ensure_ollama()
    payload = _post(f"{_ollama_host()}/api/chat", {
        "model": MODEL,
        "stream": False,
        "think": not NO_THINK,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0.3, "num_predict": 400},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": _prompt(row)},
        ],
    }, timeout)
    return _strip_thinking(payload.get("message", {}).get("content", ""))


def _explain_openai(row, timeout: int) -> str:
    """OpenAI-compatible chat completions: LM Studio, llama.cpp, vLLM."""
    system = SYSTEM + (" /no_think" if NO_THINK else "")
    payload = _post(f"{BASE_URL}/chat/completions", {
        "model": MODEL,
        "max_tokens": 700,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": _prompt(row)},
        ],
    }, timeout)
    return _strip_thinking(payload["choices"][0]["message"]["content"])


def _explain_local(row, timeout: int) -> str:
    style = os.environ.get("COACH_API_STYLE") or ("ollama" if "11434" in BASE_URL else "openai")
    return _explain_ollama(row, timeout) if style == "ollama" else _explain_openai(row, timeout)


def explain(row, api_key: str | None = None, timeout: int = 120) -> str:
    if BACKEND == "local":
        return _explain_local(row, timeout)
    return _explain_anthropic(row, api_key, timeout)


ASK_SYSTEM = (
    "You are a chess coach talking to a ~1000 rated club player who is studying "
    "a position. You are given verified facts about the position: whose move it "
    "is, the moves played, the engine's candidate moves with evaluations, material, "
    "and which pieces are loose. Answer the player's question in at most four "
    "sentences, in plain language, using those facts. "
    "The candidate moves have NOT been played yet - never describe one as if it "
    "is already on the board, and never say the player 'played' it. "
    "Do not invent tactics, squares, or variations that are not in the facts. "
    "If the facts do not settle the question, say what is uncertain rather than "
    "guessing. Talk about plans and ideas, not evaluation numbers. Prose only."
)


def ask(question: str, facts: dict, history: list | None = None, timeout: int = 180) -> str:
    """Free-form question about a position, grounded in engine-supplied facts."""
    content = json.dumps(facts, indent=1) + f"\n\nPlayer's question: {question}"
    messages = [{"role": "system", "content": ASK_SYSTEM}]
    for turn in (history or [])[-4:]:          # keep follow-ups coherent, stay small
        if turn.get("q"):
            messages.append({"role": "user", "content": turn["q"]})
        if turn.get("a"):
            messages.append({"role": "assistant", "content": turn["a"]})
    messages.append({"role": "user", "content": content})

    if BACKEND == "local":
        style = os.environ.get("COACH_API_STYLE") or ("ollama" if "11434" in BASE_URL else "openai")
        if style == "ollama":
            ensure_ollama()
            payload = _post(f"{_ollama_host()}/api/chat", {
                "model": MODEL,
                "stream": False,
                "think": not NO_THINK,
                "keep_alive": KEEP_ALIVE,
                "options": {"temperature": 0.4, "num_predict": 500},
                "messages": messages,
            }, timeout)
            return _strip_thinking(payload.get("message", {}).get("content", ""))
        payload = _post(f"{BASE_URL}/chat/completions", {
            "model": MODEL, "max_tokens": 700, "temperature": 0.4,
            "messages": messages,
        }, timeout)
        return _strip_thinking(payload["choices"][0]["message"]["content"])

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set (or use COACH_BACKEND=local)")
    payload = _post_anthropic({
        "model": MODEL,
        "max_tokens": 500,
        "system": ASK_SYSTEM,
        "messages": [m for m in messages if m["role"] != "system"],
    }, key, timeout)
    return "".join(b.get("text", "") for b in payload.get("content", [])).strip()


def _post_anthropic(body: dict, api_key: str, timeout: int) -> dict:
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def annotate_game(conn, game_id: str, classes=("blunder", "mistake"), only_mine=True) -> int:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if BACKEND != "local" and not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set (or run a local model with COACH_BACKEND=local)")

    g = conn.execute("SELECT my_color FROM games WHERE id=?", (game_id,)).fetchone()
    if g is None:
        raise RuntimeError("no such game")

    q = "SELECT * FROM positions WHERE game_id=? AND class IN ({})".format(
        ",".join("?" * len(classes)))
    args = [game_id, *classes]
    if only_mine and g["my_color"]:
        q += " AND side=?"
        args.append(g["my_color"])

    written = 0
    for row in conn.execute(q + " ORDER BY ply", args).fetchall():
        done = conn.execute(
            "SELECT 1 FROM annotations WHERE game_id=? AND ply=? AND source='llm'",
            (game_id, row["ply"]),
        ).fetchone()
        if done:
            continue
        body = explain(row, key)
        if not body:
            continue
        now = int(time.time())
        conn.execute(
            "INSERT OR REPLACE INTO annotations (game_id, ply, source, body, created_at) "
            "VALUES (?,?,'llm',?,?)", (game_id, row["ply"], body, now))
        conn.execute(
            "INSERT INTO search_fts (game_id, ply, kind, body) VALUES (?,?,'annotation',?)",
            (game_id, row["ply"], body))
        conn.commit()
        written += 1
    return written


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: llm.py <game_id>")
        raise SystemExit(2)
    conn = db.init()
    print(f"wrote {annotate_game(conn, sys.argv[1])} llm comments")
