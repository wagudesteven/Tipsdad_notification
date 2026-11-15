"""
Simulate the notifications WITHOUT sending anything to users.

    python simulate.py          real games from your API (default)
    python simulate.py demo     a made-up match day (Arsenal vs Chelsea...)

Real mode: today's games are fetched ONCE from your API, then the bot is run
every 5 minutes from now for the next 2 hours, so you see exactly what your
users would get with today's real games. (Scores don't change during the
test: it uses the games as they are right now.)

Runs the real bot (notify_loop.py -> notify.py) every 5 minutes, like
cron-job.org does, and shows which audience gets which message and when.

You'll see the cycle: 3 notifications 5 minutes apart, then a 45-minute break.
Uses Gemini if GEMINI_API_KEY is set (football facts need it), otherwise the
ready-written messages from messages.py.
"""

import contextlib
import io
import os
import sys
import time
from datetime import datetime as _dt, timedelta

os.environ["DRY_RUN"] = "1"

import notify       # noqa: E402
import notify_loop  # noqa: E402

SIM_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "state_simulation.json")
notify.STATE_FILE = SIM_STATE           # never touch the real state.json
if os.path.exists(SIM_STATE):
    os.remove(SIM_STATE)

DEMO = "demo" in sys.argv[1:]

if DEMO:
    start = _dt.now(notify.TZ).replace(hour=19, minute=20, second=0,
                                       microsecond=0)
else:
    real_now = _dt.now(notify.TZ).replace(second=0, microsecond=0)
    start = real_now - timedelta(minutes=real_now.minute % 5)
    # Fetch today's real games once, before the clock is faked
    try:
        REAL_TIPS = notify.load_recent_tips(real_now)
    except Exception as e:
        sys.exit(f"Could not load games from your API ({notify.API_URL}): {e}")
clock = {"now": start}


class FakeClock(_dt):
    @classmethod
    def now(cls, tz=None):
        return clock["now"]


notify.datetime = FakeClock
notify_loop.datetime = FakeClock

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


if DEMO:
    notify.load_recent_tips = fake_tips
else:
    notify.load_recent_tips = lambda now: REAL_TIPS


# ---- the match day: (minutes after 19:20, what happens) ----
def post_first(): posted["ids"] = {1, 2, 3}
def post_more(): posted["ids"] = {1, 2, 3, 4, 5}
def three_win(): status.update({1: "win", 2: "win", 4: "win"})
def two_lose(): status.update({3: "loss", 5: "loss"})

STORY = {
    0:   ("Admin starts posting tips (3 so far)", post_first),
    5:   ("Admin adds 2 more tips (5 in total)", post_more),
    40:  ("20:00 kickoff: all 5 matches are live", None),
    115: ("Three VIP tips WON", three_win),
    120: ("Last two tips LOST: all settled", two_lose),
}

AUDIENCE = {
    notify.ALL_USERS: "everyone",
    notify.NON_VIP: "free users",
    notify.COMBINED_SUBS: "VIP Combined",
    notify.LAPSED: "expired VIP",
    f"'{notify_loop.PULSE_TOPIC}' in topics": "everyone",
}


def who(condition: str) -> str:
    if condition in AUDIENCE:
        return AUDIENCE[condition]
    for ent, name in notify.PACKAGE_NAME.items():
        if condition == notify.package_subs(ent):
            return f"{name} subscribers"
        if condition == notify.upsell_for(ent):
            return f"upgrade offer (not {name})"
    return condition


# Print every send in a readable way
_send = notify.send


def show(msg, event, condition):
    print(f"   [{clock['now']:%H:%M}] {event:<13} -> {who(condition)}")
    print(f"                 {msg['title']} | {msg['body']}")


notify.send = show

# The simulation fires a whole evening in seconds, which the free Gemini
# quota can't keep up with. So only the football facts use Gemini here;
# the other messages come from the library. SIM_AI_WORDING=1 uses AI for all.
notify.AI_WORDING = os.getenv("SIM_AI_WORDING") == "1"

# ---- run every 5 minutes ----
if DEMO:
    TOTAL_MINUTES = 220          # 19:20 -> 23:00
else:
    STORY = {}
    TOTAL_MINUTES = 120          # the next 2 hours
    today = [t for t in REAL_TIPS if t["day"] == start.strftime("%Y-%m-%d")]
    print(f"\nGames from your API today: {len(today)}")
    for t in sorted(today, key=lambda t: t["kickoff"]):
        kind = (notify.PACKAGE_NAME.get(t["entitlement"], "VIP")
                if t["is_vip"] else "Free")
        result = ("WON" if t["won"] else "LOST" if t["lost"]
                  else "settled" if t["settled"] else "")
        print(f"   {t['kickoff']:%H:%M}  {t['home']} vs {t['away']}"
              f"  [{kind}] {result}")
    if not today:
        print("   (none: the bot will only send banter / next-kickoff messages)")
    print(f"\nSimulating {start:%H:%M} -> "
          f"{start + timedelta(minutes=TOTAL_MINUTES):%H:%M}")
for minute in range(0, TOTAL_MINUTES + 1, 5):
    clock["now"] = start + timedelta(minutes=minute)
    if minute in STORY:
        label, action = STORY[minute]
        print(f"\n===== {clock['now']:%H:%M}  {label}")
        if action:
            action()
    notify.RUN_STARTED = time.monotonic()   # each tick is a fresh run
    notify._ai_rate_limited = False
    with contextlib.redirect_stdout(io.StringIO()) as log:
        notify_loop.main()
    for line in log.getvalue().splitlines():
        if line.startswith("   ["):
            print(line)
        elif line.startswith("                 "):
            print(line)
        elif (line.startswith("Fact: ") and "sent" not in line
              and "due at" not in line):
            print(f"   [{clock['now']:%H:%M}] ({line})")

notify.send = _send
os.remove(SIM_STATE)
print("\nSimulation finished. Real state.json was not touched.")