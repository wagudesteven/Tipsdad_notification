"""
Tipsdad: a "pulse" notification every 45 minutes.

Runs every 5 minutes (cron-job.org + GitHub Actions). Each run:

  1. Runs the normal bot (notify.py): tips posted, kickoff soon, live,
     today's wins, results, live updates, VIP / upgrade / renew.
     Each of those is still sent once only.
  2. If 45 minutes have passed since the last pulse, sends ONE pulse:
     a different message about what's happening today (next kickoff,
     results so far, banter or free-tips reminder), from the
     professional library in messages.py, sometimes written by Gemini.

Live matches are covered by notify.py's own 45-minute live updates,
so pulses don't repeat them.

Settings: change the numbers below, or set them as GitHub variables.
"""

import os
import random
from datetime import datetime, timedelta

import messages
import notify

# ============================================================
# SETTINGS
# ============================================================

PULSE_EVERY_MINUTES = int(os.getenv("PULSE_EVERY_MINUTES", "45"))

# Who gets the pulses. Try "tipsdad_test" first (your phone only),
# then switch to "tipsdad" (every user).
PULSE_TOPIC = os.getenv("PULSE_TOPIC", "tipsdad")

# Share of pulses written by Gemini (the rest come from messages.py)
AI_SHARE = 0.5

QUIET_HOURS = notify.QUIET_HOURS          # 01:00–06:59: nothing is sent


# ============================================================
# WHAT TO TALK ABOUT
# ============================================================

def pulse_details(tips: list[dict], now: datetime):
    """(kind, details) for one pulse, or None if there's nothing today."""
    upcoming = sorted((t for t in tips if t["kickoff"] > now),
                      key=lambda t: t["kickoff"])
    settled = [t for t in tips if t["won"] or t["lost"]]

    options = []
    if upcoming:
        nxt = upcoming[0]["kickoff"]
        group = [t for t in upcoming if t["kickoff"] == nxt]
        featured = [t for t in group if t["is_featured"]]
        mins = max(1, round((nxt - now).total_seconds() / 60))
        hours, rest = divmod(mins, 60)
        options.append(("kickoff_soon" if mins <= 30 else "next_kickoff", {
            "matches": len(group),
            "kickoff": f"{nxt:%H:%M}",
            "minutes_to_kickoff": mins,
            "wait": (f"{hours}h {rest}m" if hours else f"{rest} min"),
            "matches_still_ahead_today": len(upcoming),
            "leagues": sorted({t["league"] for t in group if t["league"]}),
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
    if tips:
        # Banter / free-tips reminder
        options.append(("engagement", {
            "matches": len(tips),
            "kickoff": f"{min(t['kickoff'] for t in tips):%H:%M}",
        }))
    return random.choice(options) if options else None


def write_pulse(kind: str, d: dict, now: datetime, recent: list[str]) -> dict:
    if kind == "engagement":
        return messages.extras(d) or {
            "title": "Today on Tipsdad ⚽",
            "body": f"{d['matches']} matches on today's list. Open Tipsdad.",
        }
    # Countdown longer than 12 minutes ahead: use the library
    # (its countdown lines read well for any gap)
    if kind != "next_kickoff" and random.random() < AI_SHARE:
        return notify.write_message(kind, d, now, recent, False)
    return notify.fallback(kind, d)


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    # 1. Normal event notifications (each sent once only)
    notify.main()

    # 2. The 45-minute pulse
    now = datetime.now(notify.TZ)
    state = notify.load_state(now.strftime("%Y-%m-%d"))
    last = state.get("last_pulse")
    due = (last is None or now - datetime.fromisoformat(last)
           >= timedelta(minutes=PULSE_EVERY_MINUTES))

    if not last:
        if state.get("sent_today", 0) == 0:
            print("Pulse: waiting for today's first notification")
        else:
            # Start the 45-minute clock from today's first notification
            state["last_pulse"] = now.isoformat()
            notify.save_state(state)
            print(f"Pulse: clock started, first pulse in "
                  f"{PULSE_EVERY_MINUTES} min")
        return
    if now.hour in QUIET_HOURS:
        print("Pulse: quiet hours")
        return
    if not due:
        nxt = datetime.fromisoformat(last) + timedelta(minutes=PULSE_EVERY_MINUTES)
        print(f"Pulse: next one at {nxt:%H:%M}")
        return

    tips = [t for t in notify.load_recent_tips(now)
            if t["day"] == now.strftime("%Y-%m-%d")]
    picked = pulse_details(tips, now)
    if not picked:
        print("Pulse: skipped, no tips today")
        return

    kind, details = picked
    msg = write_pulse(kind, details, now, state["recent_messages"])
    notify.send(msg, kind, f"'{PULSE_TOPIC}' in topics")

    state["last_pulse"] = now.isoformat()
    state["recent_messages"] = (state["recent_messages"] +
                                [f"{msg['title']} — {msg['body']}"])[-20:]
    notify.save_state(state)
    print(f"Pulse sent ({kind}). Next one in {PULSE_EVERY_MINUTES} min.")


if __name__ == "__main__":
    main()