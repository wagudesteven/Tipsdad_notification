"""
Tipsdad: a notification every 30 seconds.

Each GitHub run lasts about 4.5 minutes; cron-job.org starts a new run
every 5 minutes, so it runs non-stop. Every 30 seconds it:

  1. Runs the normal bot (notify.py): tips posted, kickoff soon, live,
     won, results, live updates, VIP / upgrade / renew. Each of those is
     still sent once only.
  2. Sends a "pulse": a DIFFERENT message about what's happening now
     (live matches, next kickoff, today's results), written fresh by
     Gemini, with ready-written messages as a backup.

Settings: change the numbers below, or set them as GitHub variables.
notify.py is not changed.
"""

import os
import random
import time
from datetime import datetime, timedelta

import firebase_admin

import messages
import notify

# ============================================================
# SETTINGS
# ============================================================

PULSE_EVERY_SECONDS = int(os.getenv("PULSE_EVERY_SECONDS", "30"))

# Who gets the pulses. Try "tipsdad_test" first (your phone only),
# then switch to "tipsdad" (every user).
PULSE_TOPIC = os.getenv("PULSE_TOPIC", "tipsdad")

# How long one GitHub run keeps going (seconds). Keep it under 5 minutes
# so it ends before cron-job.org starts the next run.
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "270"))

# Use Gemini for at most this many pulses per run; the rest use the
# ready-written messages (protects the free Gemini limit).
AI_PULSES_PER_RUN = int(os.getenv("AI_PULSES_PER_RUN", "2"))

QUIET_HOURS = notify.QUIET_HOURS          # 01:00–06:59: nothing is sent


# ============================================================
# PULSE MESSAGES
# ============================================================

def pulse_details(tips: list[dict], now: datetime):
    """What to talk about right now: (kind, details) or None."""
    in_play = [t for t in tips
               if t["kickoff"] <= now
               < t["kickoff"] + timedelta(minutes=notify.MATCH_LENGTH_MINUTES)
               and not t["settled"]]
    upcoming = sorted((t for t in tips if t["kickoff"] > now),
                      key=lambda t: t["kickoff"])
    settled = [t for t in tips if t["won"] or t["lost"]]

    options = []
    if in_play:
        random.shuffle(in_play)
        options.append(("live_update", {
            "live_now": len(in_play),
            "matches_you_may_name": notify.match_names(in_play),
        }))
    if upcoming:
        nxt = upcoming[0]["kickoff"]
        group = [t for t in upcoming if t["kickoff"] == nxt]
        featured = [t for t in group if t["is_featured"]]
        mins = max(1, round((nxt - now).total_seconds() / 60))
        options.append(("kickoff_soon", {
            "matches": len(group),
            "kickoff": f"{nxt:%H:%M}",
            "minutes_to_kickoff": mins,
            "matches_still_ahead_today": len(upcoming),
            "matches_you_may_name": notify.match_names(featured),
        }))
    if settled:
        won = sum(1 for t in settled if t["won"])
        options.append(("results", {
            "day": "today so far",
            "won": won,
            "lost": len(settled) - won,
            "matches_you_may_name": notify.match_names(
                [t for t in settled if t["won"]]),
        }))
    return random.choice(options) if options else None


FALLBACK_PULSES = {
    "live_update": [
        ("🔴 Live now", "{n} match(es) on right now{names}. Follow every minute on Tipsdad."),
        ("⚽ Still playing", "{n} game(s) in progress{names}. Open Tipsdad for live scores."),
        ("🔥 Action on", "{names_or_n} are live. Don't miss the finish on Tipsdad."),
        ("⏱ Clock is running", "{n} match(es) live{names}. Check them on Tipsdad now."),
    ],
    "kickoff_soon": [
        ("⏳ Next kickoff {k}", "{n} match(es) start in {m} min. Get ready on Tipsdad."),
        ("Kickoff at {k} ⚽", "{m} minutes to go. Today's tips are waiting on Tipsdad."),
        ("Don't be late ⏰", "Next games start at {k}. Open Tipsdad before kickoff."),
        ("Coming up {k} 🔜", "{ahead} more match(es) still to play today on Tipsdad."),
    ],
    "results": [
        ("📊 Today so far", "{won} won, {lost} lost. More matches still to come on Tipsdad."),
        ("Results update ✅", "{won} tip(s) won today so far. See them in History."),
        ("Scoreboard 📈", "Today: {won} won, {lost} lost. Open Tipsdad for the details."),
    ],
}


def fallback_pulse(kind: str, d: dict) -> dict:
    # Professional library first (weekend / big-competition aware)
    msg = messages.render(kind, d, datetime.now(notify.TZ))
    if msg:
        return msg
    names = d.get("matches_you_may_name") or []
    title, body = random.choice(FALLBACK_PULSES[kind])
    fill = {
        "n": d.get("live_now") or d.get("matches") or "",
        "names": f" including {names[0]}" if names else "",
        "names_or_n": " and ".join(names[:2]) if names
        else f"{d.get('live_now', '')} match(es)",
        "k": d.get("kickoff", ""),
        "m": d.get("minutes_to_kickoff", ""),
        "ahead": d.get("matches_still_ahead_today", ""),
        "won": d.get("won", 0),
        "lost": d.get("lost", 0),
    }
    return {"title": title.format(**fill), "body": body.format(**fill)[:160]}


# ============================================================
# LOOP
# ============================================================

def main() -> None:
    started = time.monotonic()

    # Firebase once for the whole loop (notify.main would re-init it)
    if not notify.DRY_RUN and not firebase_admin._apps:
        notify.init_firebase()
    notify.init_firebase = lambda: None

    recent: list[str] = []
    ai_used = 0
    pulses = 0
    print(f"Pulses every {PULSE_EVERY_SECONDS}s to '{PULSE_TOPIC}' "
          f"for {LOOP_SECONDS}s.")

    while time.monotonic() - started < LOOP_SECONDS:
        tick = time.monotonic()
        now = datetime.now(notify.TZ)

        # 1. Normal event notifications (each sent once only)
        try:
            notify.RUN_STARTED = time.monotonic()
            notify.main()
        except Exception as e:
            print(f"notify.main failed this round: {e}")

        # 2. The 30-second pulse
        if now.hour not in QUIET_HOURS:
            try:
                tips = [t for t in notify.load_recent_tips(now)
                        if t["day"] == now.strftime("%Y-%m-%d")]
                picked = pulse_details(tips, now)
                # About 1 in 5 pulses: banter or free-user engagement
                if picked and random.random() < 0.2:
                    extra = messages.extras(picked[1])
                    if extra:
                        notify.send(extra, "engagement",
                                    f"'{PULSE_TOPIC}' in topics")
                        recent = (recent + [f"{extra['title']} — "
                                            f"{extra['body']}"])[-30:]
                        pulses += 1
                        picked = None
                if picked:
                    kind, details = picked
                    if ai_used < AI_PULSES_PER_RUN:
                        msg = notify.write_message(kind, details, now,
                                                   recent, False)
                        ai_used += 1
                    else:
                        msg = fallback_pulse(kind, details)
                    # Never repeat a message already sent in this run
                    tries = 0
                    while (f"{msg['title']} — {msg['body']}" in recent
                           and tries < 5):
                        msg = fallback_pulse(kind, details)
                        tries += 1
                    notify.send(msg, kind, f"'{PULSE_TOPIC}' in topics")
                    recent = (recent + [f"{msg['title']} — {msg['body']}"])[-30:]
                    pulses += 1
                else:
                    print("Pulse skipped: no tips today")
            except Exception as e:
                print(f"Pulse failed this round: {e}")

        # Wait for the next 30-second slot
        left = PULSE_EVERY_SECONDS - (time.monotonic() - tick)
        if left > 0 and time.monotonic() - started + left < LOOP_SECONDS:
            time.sleep(left)
        elif left > 0:
            break

    print(f"Loop finished: {pulses} pulse(s) sent.")


if __name__ == "__main__":
    main()