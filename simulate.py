"""
Simulate a full match day WITHOUT your API and WITHOUT sending anything.
Shows exactly which audience gets which message, step by step.

    python simulate.py

Uses the real notify.py logic (and Gemini if GEMINI_API_KEY is set,
otherwise the built-in fallback messages).
"""

import os
from datetime import datetime as _dt, timedelta

os.environ["DRY_RUN"] = "1"

import notify  # noqa: E402

STATE = notify.STATE_FILE
SIM_STATE = os.path.join(os.path.dirname(__file__), "state_simulation.json")
notify.STATE_FILE = SIM_STATE           # never touch the real state.json
if os.path.exists(SIM_STATE):
    os.remove(SIM_STATE)

start = _dt.now(notify.TZ).replace(hour=19, minute=20, second=0, microsecond=0)
clock = {"now": start}


class FakeClock(_dt):
    @classmethod
    def now(cls, tz=None):
        return clock["now"]


notify.datetime = FakeClock

kickoff = start + timedelta(minutes=40)    # all matches at 20:00
day = kickoff.strftime("%Y-%m-%d")

# id, home, away, package entitlement (None = free), featured
MATCHES = [
    (1, "Arsenal", "Chelsea", "vipgg", True),
    (2, "Getafe", "Elche", "vipdraws", False),
    (3, "Roma", "Lazio", None, False),
    (4, "Lyon", "Nice", "vipgg", False),
    (5, "Ajax", "PSV", "vipcorrectscore", False),
]
status = {m[0]: "pending" for m in MATCHES}
posted = {"ids": set()}


def fake_tips(now):
    out = []
    for i, home, away, ent, featured in MATCHES:
        if i not in posted["ids"]:
            continue
        s = status[i]
        out.append({
            "id": i, "kickoff": kickoff, "is_vip": ent is not None,
            "is_featured": featured, "entitlement": ent,
            "league": "Test League", "home": home, "away": away, "day": day,
            "won": s in notify.WON, "lost": s in notify.LOST,
            "settled": s in notify.WON | notify.LOST | notify.VOID,
        })
    return out


notify.load_recent_tips = fake_tips


def step(minutes: int, label: str) -> None:
    clock["now"] += timedelta(minutes=minutes)
    print(f"\n===== {clock['now']:%H:%M}  {label}")
    notify.main()


# ---- the match day ----
posted["ids"] = {1, 2, 3}
step(0, "Admin starts posting tips (3 so far)")
posted["ids"] = {1, 2, 3, 4, 5}
step(5, "Admin adds 2 more tips (5 in total)")
step(10, "10 min since the last new tip: still waiting")
step(10, "20 min with no new tips → 'tips posted'")
step(7, "8 min to kickoff → 'kickoff soon'")
step(10, "Matches kicked off → live alerts per audience")
status[1] = status[4] = "win"
status[2] = "win"
step(105, "Three VIP tips won → wins, upgrade, renew")
status[3] = "loss"
status[5] = "loss"
step(5, "Last tips settled (still updating)")
step(15, "15 min with no changes → results summary")
step(5, "Next check: nothing new, nothing repeated")

os.remove(SIM_STATE)
print("\nSimulation finished. Real state.json was not touched.")