"""
Tipsdad: 3 notifications every 45 minutes.

Runs every 5 minutes (cron-job.org + GitHub Actions). Each run:

  1. Runs the bot (notify.py): "tips posted", results summary, and the
     first two notifications of each cycle, built from the app's games:
        0 min    results update: tips that WON since the last cycle,
                 or today's real record when nothing new won
        +5 min   live update: matches live now, kicking off soon,
                 or the next kickoff later today
  2. Sends the third one itself:
        +10 min  an AI football FACT about ONE of today's games, found with
                 Google Search (derby, last head-to-head, form, unbeaten
                 run, table position, milestone...). Only facts the AI
                 confirmed through search are sent. No confirmed fact ->
                 a banter line from messages.py instead.
  3. Then a 45-minute break after the 3rd, and the cycle repeats:
        0, 5, 10 ... (45 min) ... 55, 60, 65 ... (45 min) ... 110, 115, 120 ...

No notification ever contains a tip, market, prediction or odds.

Settings: change the numbers below, or set them as GitHub variables.
"""

import json
import os
import random
import re
import time
from datetime import datetime, timedelta

import requests

import messages
import notify

# ============================================================
# SETTINGS
# ============================================================

# Who gets the AI facts. Try "tipsdad_test" first (your phone only),
# then switch to "tipsdad" (every user).
PULSE_TOPIC = os.getenv("PULSE_TOPIC", "tipsdad")

FACT_SLOT_MINUTES = notify.FACT_SLOT_MINUTES      # +10 min into each cycle
SLACK = notify.PULSE_SLACK_MINUTES
QUIET_HOURS = notify.QUIET_HOURS                  # 01:00–06:59: nothing is sent
FACT_TIMEOUT_SECONDS = 30
# AI football facts per day. 0 = AI OFF: Gemini is never contacted and every
# 3rd slot is a ready-written line. Set AI_FACTS_PER_DAY=2 to turn it back on.
# When on, facts are spread out: at least AI_FACT_GAP_MINUTES apart.
AI_FACTS_PER_DAY = int(os.getenv("AI_FACTS_PER_DAY", "0"))
AI_FACT_GAP_MINUTES = int(os.getenv("AI_FACT_GAP_MINUTES", "120"))

# Angles the AI rotates through, so facts stay different
ANGLES = [
    "derby or rivalry (why this match matters to both clubs)",
    "the last head-to-head meeting (score and when)",
    "current form (results of the last 5 matches)",
    "an unbeaten or winless run going into this match",
    "league table positions and what is at stake",
    "a key player's recent scoring record",
    "a historic or record fact about this fixture",
    "home or away record this season",
]

FACT_PROMPT = """You write ONE short football fact as a push notification
for Tipsdad, a football app.

Use Google Search to look up the match you're given, then write ONE true,
current, interesting fact about it, preferably from the suggested angle.

Rules:
- Return ONLY JSON: {"title": "...", "body": "...", "angle": "..."}
- Title max 40 characters, body max 110 characters, one emoji max.
- Name the match as "Team A vs Team B" in the title or body.
- The fact must come from your search results. Numbers, dates and scores
  must be exact. If you cannot confirm a fact, return {"skip": true}.
- NEVER give a prediction, tip, pick, market, odds or a "who will win".
  No betting words ("bet", "stake", "sure", "banker", "guaranteed").
- Fresh wording: don't repeat the recent notifications you're shown.
- Friendly, punchy, the kind of line a football fan wants to read.
- End by pointing to the match, e.g. "Kickoff 21:00 on Tipsdad"."""


# ============================================================
# PICK A GAME
# ============================================================

def fact_candidates(tips: list[dict], now: datetime) -> list[dict]:
    """Games we may name: featured (any time), free games, and live games.
    VIP games are only named once they're live."""
    out = []
    for t in tips:
        if not (t["home"] and t["away"]) or t["settled"]:
            continue
        live = t["kickoff"] <= now < t["kickoff"] + \
            timedelta(minutes=notify.MATCH_LENGTH_MINUTES)
        upcoming = t["kickoff"] > now
        if not (live or upcoming):
            continue
        if t["is_featured"] or not t["is_vip"] or live:
            out.append(t)
    # Soonest first, live games right away
    return sorted(out, key=lambda t: t["kickoff"])


def pick_game(tips: list[dict], now: datetime, history: list[dict]):
    games = fact_candidates(tips, now)
    if not games:
        return None, None
    name = lambda g: f"{g['home']} vs {g['away']}"
    today = [h["match"] for h in history if h.get("day") == f"{now:%Y-%m-%d}"]
    last = history[-1]["match"] if history else None
    # Not more than 2 facts per match per day, never the same match twice in a row
    allowed = [g for g in games if today.count(name(g)) < 2 and name(g) != last] \
        or [g for g in games if today.count(name(g)) < 2] or []
    if not allowed:
        return None, None
    fresh = [g for g in allowed if name(g) not in today]
    pool = fresh or allowed
    game = pool[0] if random.random() < 0.6 else random.choice(pool)
    recent_angles = [h.get("angle") for h in history[-len(ANGLES) + 2:]]
    angles = [a for a in ANGLES if a not in recent_angles] or ANGLES
    return game, random.choice(angles)


# ============================================================
# ASK GEMINI (with Google Search)
# ============================================================

def ai_fact(game: dict, angle: str, now: datetime, recent: list[str]):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Fact: no GEMINI_API_KEY, using banter instead")
        return None

    match = f"{game['home']} vs {game['away']}"
    live = game["kickoff"] <= now
    prompt = (
        f"Match: {match}\n"
        f"Competition: {game['league'] or 'unknown'}\n"
        f"Kickoff: {game['kickoff']:%A %d %B %Y, %H:%M} (Nairobi time)"
        f"{' (already LIVE now)' if live else ''}\n"
        f"Now: {now:%A %d %B %Y, %H:%M}\n"
        f"Suggested angle: {angle}\n"
        f"Recent notifications (do NOT repeat):\n- "
        + "\n- ".join(recent[-10:] or ["(none)"])
    )
    payload = {
        "systemInstruction": {"parts": [{"text": FACT_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 500},
    }
    try:
        res = notify.gemini_post(payload, api_key, FACT_TIMEOUT_SECONDS,
                                 log="Fact: ")
        cand = res.json()["candidates"][0]
        grounding = cand.get("groundingMetadata") or {}
        searched = bool(grounding.get("groundingChunks")
                        or grounding.get("webSearchQueries"))
        text = "".join(p.get("text", "") for p in cand["content"]["parts"])
        start, end = text.find("{"), text.rfind("}")
        msg = json.loads(text[start:end + 1])
    except Exception as e:
        print(f"Fact: AI failed ({str(e).replace(api_key, '***')}), using banter instead")
        return None

    if msg.get("skip") or not searched:
        print("Fact: no confirmed fact from search, using banter instead")
        return None
    title, body = str(msg.get("title", "")).strip(), str(msg.get("body", "")).strip()
    banned = re.compile(r"\b(bet|bets|betting|stake|odds|banker|sure win|"
                        r"guarantee\w*|tip:|prediction)\b", re.IGNORECASE)
    if not title or not body or banned.search(title + " " + body):
        print("Fact: AI message failed the safety check, using banter instead")
        return None
    return {"title": title[:60], "body": body[:160],
            "angle": str(msg.get("angle") or angle)[:60]}


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    # 1. Events + cycle slots +0 (results) and +15 (live), from the app's games
    notify.main()

    # 2. Cycle slot +30: the AI football fact
    now = datetime.now(notify.TZ)
    state = notify.load_state(now.strftime("%Y-%m-%d"))
    cyc = state.get("cycle") or {}
    if not cyc.get("at"):
        print("Fact: waiting for the first cycle")
        return
    if now.hour in QUIET_HOURS:
        print("Fact: quiet hours")
        return
    mins = (now - datetime.fromisoformat(cyc["at"])).total_seconds() / 60
    if "fact" in cyc["done"]:
        return
    if mins < FACT_SLOT_MINUTES - SLACK:
        print(f"Fact: due at "
              f"{datetime.fromisoformat(cyc['at']) + timedelta(minutes=FACT_SLOT_MINUTES):%H:%M}")
        return
    sent = state["sent_by_audience"].get("fact", 0)
    if sent >= notify.MAX_PER_DAY:
        print("Fact: daily limit reached")
        cyc["done"].append("fact")
        cyc["ended"] = now.isoformat()
        notify.save_state(state)
        return

    # Mark the slot first, so a stopped run can never send it twice.
    # The 45-minute break is counted from here.
    cyc["done"].append("fact")
    cyc["ended"] = now.isoformat()
    state["cycle"] = cyc
    notify.save_state(state)

    tips = [t for t in notify.load_recent_tips(now)
            if t["day"] == now.strftime("%Y-%m-%d")]
    history = state.setdefault("fact_history", [])
    game, angle = pick_game(tips, now, history)

    # Daily AI allowance: AI_FACTS_PER_DAY facts, spread out. Failed tries
    # count too (max twice the allowance), so a broken key can't use quota
    # all day.
    today = f"{now:%Y-%m-%d}"
    facts_today = [h for h in history if h.get("day") == today]
    tries = state.get("ai_tries") or {}
    if tries.get("day") != today:
        tries = {"day": today, "n": 0}
    last_at = max((h["at"] for h in facts_today if h.get("at")), default=None)
    wait_ok = (last_at is None or
               (now - datetime.fromisoformat(last_at)).total_seconds() / 60
               >= AI_FACT_GAP_MINUTES)
    use_ai = (game is not None
              and len(facts_today) < AI_FACTS_PER_DAY
              and tries["n"] < 2 * AI_FACTS_PER_DAY
              and wait_ok)
    if game and not use_ai and AI_FACTS_PER_DAY > 0:
        if len(facts_today) >= AI_FACTS_PER_DAY:
            print(f"Fact: today's {AI_FACTS_PER_DAY} AI facts already sent, "
                  "using banter")
        elif not wait_ok:
            print("Fact: last AI fact was less than "
                  f"{AI_FACT_GAP_MINUTES} min ago, using banter")
        else:
            print("Fact: too many failed AI tries today, using banter")

    msg = None
    if use_ai:
        tries["n"] += 1
        state["ai_tries"] = tries
        msg = ai_fact(game, angle, now, state["recent_messages"])
    kind = "fact"
    if msg:
        history.append({"day": today, "at": now.isoformat(),
                        "match": f"{game['home']} vs {game['away']}",
                        "angle": angle})
        state["fact_history"] = history[-30:]
    elif tips:
        # No confirmed fact: a banter / free-tips line instead
        kind = "banter"
        msg = messages.extras({
            "matches": len(tips),
            "kickoff": f"{min(t['kickoff'] for t in tips):%H:%M}",
        })
    if not msg:
        print("Fact: nothing to send (no games today)")
        return

    notify.send(msg, kind, f"'{PULSE_TOPIC}' in topics")
    state["sent_by_audience"]["fact"] = sent + 1
    state["sent_today"] += 1
    state["recent_messages"] = (state["recent_messages"] +
                                [f"{msg['title']} — {msg['body']}"])[-20:]
    notify.save_state(state)
    print(f"Fact slot sent ({kind}).")


if __name__ == "__main__":
    main()