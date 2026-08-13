"""Who actually decided each game: you, or your opponent falling over?

For every decided game we walk the eval curve from your point of view and find
the "clinch" - the first ply after which the game never comes back. Then we ask
what happened on that ply:

  earned    you were already winning on your own play before any gift arrived
  gifted    a single opponent blunder handed you a position you did not build
  off-board you won without ever being winning (they flagged or resigned early)

Losses get the mirror treatment. Usage:  python win_source.py [rapid|blitz|...]
"""

import sys
from collections import Counter

import db

CLINCH_CP = 300      # "winning" threshold
HOLD_CP = 0          # must never fall back below this afterwards
GIFT_CP = 150        # an opponent error this size is a gift, not your doing
EDGE_CP = 100        # above this you already had something real


def eval_curve(rows, my_color: str) -> list[int]:
    """Eval after each ply, always from my point of view, in centipawns."""
    out = []
    for r in rows:
        cp = r["cp_after"]
        if r["mate_after"] is not None:
            # mate_after == 0 means this move IS the mate, i.e. good for the mover.
            cp = 1000 if r["mate_after"] >= 0 else -1000
        if cp is None:
            cp = 0
        out.append(cp if r["side"] == my_color else -cp)
    return out


def clinch_index(curve: list[int], sign: int) -> int | None:
    """First ply after which the game is decided and stays decided.

    sign=+1 looks for a winning advantage for me, -1 for the opponent.
    """
    for i, v in enumerate(curve):
        if sign * v >= CLINCH_CP and all(sign * later >= HOLD_CP for later in curve[i:]):
            return i
    return None


def classify(conn, game) -> dict:
    rows = conn.execute(
        "SELECT ply, side, class, cp_after, cp_loss, mate_after FROM positions "
        "WHERE game_id=? ORDER BY ply", (game["id"],)).fetchall()
    if not rows:
        return {"bucket": "unanalyzed"}

    me = game["my_color"]
    curve = eval_curve(rows, me)
    won = game["my_result"] == "win"
    sign = 1 if won else -1

    k = clinch_index(curve, sign)
    opp_errors = [r for r in rows if r["side"] != me and (r["cp_loss"] or 0) >= GIFT_CP]
    my_errors = [r for r in rows if r["side"] == me and (r["cp_loss"] or 0) >= GIFT_CP]

    if game["my_result"] == "draw":
        return {"bucket": "draw", "clinch_ply": None,
                "opp_big_errors": len(opp_errors), "my_big_errors": len(my_errors),
                "opp_cp_given": sum(r["cp_loss"] or 0 for r in rows if r["side"] != me),
                "my_cp_given": sum(r["cp_loss"] or 0 for r in rows if r["side"] == me),
                "final_eval": curve[-1] if curve else 0}

    if k is None:
        # Never decisive on the board: the result came from the clock or a
        # resignation in a position that was not actually lost.
        bucket = "off-board-win" if won else "off-board-loss"
        culprit = None
    else:
        mover_is_me = rows[k]["side"] == me
        before = curve[k - 1] if k > 0 else 0
        loss = rows[k]["cp_loss"] or 0
        if not mover_is_me and loss >= GIFT_CP and sign * before <= EDGE_CP:
            bucket = "gifted" if won else "self-inflicted"
        elif mover_is_me and loss >= GIFT_CP and won is False:
            bucket = "self-inflicted"
        else:
            bucket = "earned" if won else "outplayed"
        culprit = rows[k]["ply"]

    return {
        "bucket": bucket,
        "clinch_ply": culprit,
        "opp_big_errors": len(opp_errors),
        "my_big_errors": len(my_errors),
        "opp_cp_given": sum(r["cp_loss"] or 0 for r in rows if r["side"] != me),
        "my_cp_given": sum(r["cp_loss"] or 0 for r in rows if r["side"] == me),
        "final_eval": curve[-1] if curve else 0,
    }


def report(conn, time_class: str | None = None) -> None:
    q = ("SELECT * FROM games WHERE analyzed_at IS NOT NULL AND my_color IS NOT NULL "
         "AND my_result IS NOT NULL")
    args = []
    if time_class:
        q += " AND time_class=?"
        args.append(time_class)
    games = conn.execute(q, args).fetchall()

    per_color = {"white": Counter(), "black": Counter()}
    totals = Counter()
    gift_cp = {"white": [0, 0], "black": [0, 0]}   # [opp gave, I gave]
    term = {"white": Counter(), "black": Counter()}

    for g in games:
        res = classify(conn, g)
        if res["bucket"] == "unanalyzed":
            continue
        per_color[g["my_color"]][res["bucket"]] += 1
        totals[res["bucket"]] += 1
        gift_cp[g["my_color"]][0] += res["opp_cp_given"]
        gift_cp[g["my_color"]][1] += res["my_cp_given"]
        if res["bucket"].startswith("off-board"):
            t = (g["termination"] or "").lower()
            kind = ("time" if "time" in t else
                    "resignation" if "resign" in t else
                    "checkmate" if "checkmate" in t else "other")
            term[g["my_color"]][f"{res['bucket']}:{kind}"] += 1

    label = time_class or "all speeds"
    print(f"=== how games were decided ({label}, {sum(totals.values())} games) ===\n")
    for color in ("white", "black"):
        c = per_color[color]
        wins = c["earned"] + c["gifted"] + c["off-board-win"]
        losses = c["outplayed"] + c["self-inflicted"] + c["off-board-loss"]
        n = wins + losses + c["draw"]
        if not n:
            continue
        print(f"as {color} ({n} decided games)")
        if wins:
            print(f"  wins   {wins:3}  | earned {c['earned']:3}"
                  f"  gifted {c['gifted']:3}  off-board {c['off-board-win']:3}"
                  f"   -> {100*c['earned']/wins:.0f}% earned")
        if losses:
            print(f"  losses {losses:3}  | outplayed {c['outplayed']:3}"
                  f"  self-inflicted {c['self-inflicted']:3}"
                  f"  off-board {c['off-board-loss']:3}"
                  f"   -> {100*c['self-inflicted']/losses:.0f}% self-inflicted")
        if c["draw"]:
            print(f"  draws  {c['draw']:3}")
        if term[color]:
            print("  off-board endings: " + ", ".join(
                f"{k}={v}" for k, v in sorted(term[color].items())))
        given, leaked = gift_cp[color]
        print(f"  centipawns: opponents gave you {given}, you gave away {leaked}"
              f"  (net {given - leaked:+})")
        print()


if __name__ == "__main__":
    conn = db.init()
    report(conn, sys.argv[1] if len(sys.argv) > 1 else None)
