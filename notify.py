"""
Tipsdad automatic push notifications.

Run every 5 minutes (GitHub Actions or a cPanel cron). Each run:
  1. Loads today's tips from the Tipsdad API.
  2. Sends at most one notification per event:
       • "tips posted"  – once today's tips stop changing for a while
       • "kickoff soon" – shortly before a group of matches kicks off
       • "live"         – when that group of matches has kicked off
       • "won"          – as soon as TODAY's tips are marked Won (batched)
       • "results"      – once ALL of a day's tips are settled: won/lost
       • "live_update"  – every 45 minutes while any match is in play
  3. Every message is written fresh by Google Gemini (free tier), aware
     of weekends, universal festive days and football occasions.

Audiences (Firebase topics the app joins, see subscription_topics.dart):
  • everyone ............ tips posted, results
  • non-VIP users ....... general live / won alerts
  • package subscribers . live / won alerts for THEIR package
  • VIP Combined ........ one live / won alert covering all packages
  • single-package subs  "upgrade to VIP Combined" when another package wins
  • expired subscribers . "renew your VIP" after wins

What a notification may contain:
  • Team names of FEATURED games (any time) and of LIVE games
  • Match counts, kickoff times, leagues
It NEVER contains a tip, prediction, market or odds.
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import firebase_admin

import messages
from firebase_admin import credentials, messaging

# ============================================================
# SETTINGS
# ============================================================

API_URL = "https://livescore.sprofitconcepts.com/api/tips"
FCM_TOPIC = "tipsdad"                      # the app subscribes to this
ANDROID_CHANNEL_ID = "tipsdad_alerts"      # same as the app's channel
TZ = ZoneInfo("Africa/Nairobi")            # tip times are Nairobi time
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# Free Google Gemini model (override with a GEMINI_MODEL variable)
AI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")

POSTED_QUIET_MINUTES = 20   # "tips posted" once no new tips for this long
SOON_WINDOW_MINUTES = 12    # "kickoff soon" when a match starts within this
LIVE_WINDOW_MINUTES = 20    # "live" up to this long after kickoff
MAX_PER_DAY = 14            # never send more than this in one day
QUIET_HOURS = range(1, 7)   # no notifications 01:00–06:59 Nairobi time

LIVE_UPDATE_EVERY_MINUTES = 45   # "matches still live" reminder interval
MATCH_LENGTH_MINUTES = 110       # a match counts as in play this long
MAX_LIVE_UPDATES_PER_DAY = 12    # separate from the other daily limits

RESULTS_QUIET_MINUTES = 15  # results summary once statuses stop changing
MAX_WIN_ALERTS_PER_DAY = 6  # "won" alerts per day (more go in the summary)

WON = {"won", "win", "correct"}
LOST = {"lost", "lose", "loss", "wrong", "failed"}
VOID = {"draw", "void", "postponed", "cancelled", "canceled"}

# ---------- VIP packages (must match PurchaseApi in the app) ----------
# Backend category -> RevenueCat entitlement
ENTITLEMENT_FOR = {
    "rollovermasterclass": "vipunderover",
    "htft": "vipgg",
    "daily": "vipfinalwinner",
    "draws": "vipdraws",
    "premium": "vipcorrectscore",
}
COMBINED = "vipcombined"
PACKAGE_NAME = {
    "vipunderover": "VIP Under/Over",
    "vipgg": "VIP GG",
    "vipfinalwinner": "VIP Final Winner",
    "vipdraws": "VIP Draws",
    "vipcorrectscore": "VIP Correct Score",
    COMBINED: "VIP Combined",
}

MAX_UPSELL_PER_DAY = 1      # "upgrade to VIP Combined" messages per day
LAPSED_EVERY_DAYS = 2       # "renew your VIP" at most every N days

# ---------- Audiences (Firebase topic conditions) ----------
ALL_USERS = f"'{FCM_TOPIC}' in topics"
NON_VIP = f"'{FCM_TOPIC}' in topics && !('vip_active' in topics)"
LAPSED = "'vip_lapsed' in topics"
COMBINED_SUBS = f"'vip_{COMBINED}' in topics"


def package_subs(ent: str) -> str:
    """Subscribers of one package who are NOT on VIP Combined
    (Combined users get their own single message)."""
    return f"'vip_{ent}' in topics && !('vip_{COMBINED}' in topics)"


def upsell_for(ent: str) -> str:
    """Single-package subscribers who DON'T have this package."""
    return f"'upsell_combined' in topics && !('vip_{ent}' in topics)"


# Featured (pinned) categories, same as the app's Featured section
FEATURED_CATEGORIES = {"special"}

# Most matches named in one notification
MAX_NAMED_MATCHES = 3

AI_TIMEOUT_SECONDS = 20    # max wait for one AI message
AI_BUDGET_SECONDS = 90     # total AI time per run; after that, use the
                           # ready-written messages so the run never stalls

DRY_RUN = os.getenv("DRY_RUN") == "1"      # print instead of sending

RUN_STARTED = time.monotonic()


# ============================================================
# STATE (what was already sent today)
# ============================================================

def load_state(today: str) -> dict:
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    recent = state.get("recent_messages", [])[-20:]
    # Kept across days (late matches finish after midnight)
    keep = {k: state.get(k) for k in
            ("won_ids", "settled_seen", "summaries", "settle_change",
             "last_lapsed", "last_live_update", "last_pulse")
            if k in state}
    if state.get("date") != today:
        # New day: start fresh but remember recent wording + results
        state = {"date": today, "recent_messages": recent, **keep}

    state.setdefault("known_ids", [])
    state.setdefault("last_change", None)
    state.setdefault("posted_sent", 0)
    state.setdefault("sent_keys", [])
    state.setdefault("sent_today", 0)
    state.setdefault("recent_messages", recent)
    state.setdefault("won_ids", [])          # wins already announced
    state.setdefault("settled_seen", {})     # tip id -> last status seen
    state.setdefault("summaries", [])        # days whose results were sent
    state.setdefault("settle_change", {})    # day -> last status change
    state.setdefault("win_alerts_today", 0)
    state.setdefault("sent_by_audience", {})  # audience -> count today
    state.setdefault("upsell_today", 0)
    state.setdefault("last_lapsed", None)     # date of last "renew" message
    state.setdefault("last_live_update", None)  # time of last live update
    state.setdefault("live_updates_today", 0)

    # Keep these lists small
    state["won_ids"] = state["won_ids"][-400:]
    state["summaries"] = state["summaries"][-10:]
    return state


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ============================================================
# TIPS
# ============================================================

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def kickoff_of(tip: dict):
    """'11 November 2026' + '22:00:00' -> datetime in Nairobi time."""
    try:
        day, month, year = tip["date"].split()
        hour, minute = tip["time"].split(":")[:2]
        m = month.lower()
        m = MONTHS.get(m) or next(v for k, v in MONTHS.items()
                                  if k.startswith(m[:3]))
        return datetime(int(year), m, int(day),
                        int(hour), int(minute), tzinfo=TZ)
    except Exception:
        return None


def load_recent_tips(now: datetime) -> list[dict]:
    """Today's and yesterday's tips (yesterday for late results)."""
    res = requests.get(API_URL, headers={"Accept": "application/json"},
                       timeout=30)
    res.raise_for_status()
    body = res.json()

    tips = []
    for group in body.get("data", []):
        ctype = str(group.get("category_type", "")).upper()   # VIP / FREE
        cname = str(group.get("category_name", "")).lower()
        for t in group.get("tips", []):
            k = kickoff_of(t)
            if k is None:
                continue
            days_ago = (now.date() - k.date()).days
            if days_ago not in (0, 1):
                continue
            status = str(t.get("status", "")).strip().lower()
            # Only what a notification may use: NO tip / odds fields
            tips.append({
                "id": t.get("id"),
                "kickoff": k,
                "is_vip": ctype == "VIP",
                "is_featured": cname in FEATURED_CATEGORIES,
                # VIP package this tip belongs to (None for free tips)
                "entitlement": ENTITLEMENT_FOR.get(cname)
                if ctype == "VIP" else None,
                "day": k.strftime("%Y-%m-%d"),
                "won": status in WON,
                "lost": status in LOST,
                "settled": status in WON | LOST | VOID,
                "league": t.get("league_name", ""),
                "home": t.get("team_one", ""),
                "away": t.get("team_two", ""),
            })
    return tips


def match_names(tips: list[dict]) -> list[str]:
    """'Team A vs Team B' for up to MAX_NAMED_MATCHES matches.
    Team names only: never the tip, market or odds."""
    names = []
    for t in tips:
        if t["home"] and t["away"]:
            name = f"{t['home']} vs {t['away']}"
            if name not in names:
                names.append(name)
        if len(names) >= MAX_NAMED_MATCHES:
            break
    return names


# ============================================================
# AI MESSAGE WRITER (Google Gemini, free tier)
# ============================================================

SYSTEM_PROMPT = """You write push notifications for Tipsdad, a football
tips app.

Rules:
- Return ONLY JSON: {"title": "...", "body": "..."}
- Title max 40 characters, body max 110 characters. One emoji max.
- Friendly, energetic, natural English with URGENCY: short punchy
  phrases, action words ("now", "don't miss", "kicking off", "just
  landed", "hurry"), the kind of message people open immediately.
  Urgency must stay honest: no fake deadlines or invented numbers.
- Every message must be different from the recent ones you're shown:
  new wording, new angle, new opening.
- NEVER include a tip, prediction, pick, market (e.g. "Over 2.5", "GG",
  "Home win", "Draw", "BTTS") or odds. Not even a hint of one.
- You may name teams ONLY from "matches_you_may_name" in the details,
  written as "Team A vs Team B". Never name any other team.
- Otherwise talk in counts and times (e.g. "6 matches kick off at 21:00").
- For "won" events you may say the named matches' tips WON (✅), but
  never say what the tip was.
- "live_update" is a reminder sent while matches are still being played:
  say how many are live right now and name the allowed ones. Fresh,
  exciting wording each time ("still on", "second half", "tense finish").
- For "results" events report the real numbers exactly (won and lost).
  Be honest about losses, never hide or spin them; keep the energy by
  pointing to the next tips ("next tips drop soon, stay ready").
- "vip_live" / "vip_won" go to PAYING subscribers of "package": speak
  to them as members ("your VIP GG match is live", "your VIP tip won").
- "upsell" goes to people who already pay for one package: tell them
  "package" just won and that VIP Combined unlocks every package.
  Friendly, not pushy. Never mention prices.
- "resubscribe" goes to people whose VIP ended: tell them how VIP did
  (real numbers from the details) and invite them to renew. Never
  shame them, never mention prices.
- NEVER promise wins, guaranteed profit or "sure" results, and never
  suggest betting is a way to make money or recover losses.
- Occasions you may use, only when they genuinely apply today:
  • Weekends.
  • Universal festive days: Christmas, Boxing Day, New Year, Easter,
    Valentine's Day, Eid.
  • Football occasions: derby days, Champions League / Europa League
    nights, cup finals, the opening or final day of a league season,
    international breaks, World Cup / AFCON / Euros matches, transfer
    deadline day, Boxing Day fixtures.
- Do NOT mention any country's national or public holidays.
- If none of these apply, don't invent one; just write a good message.
- If a big football story or big match is happening today, you may
  reference it briefly. Don't make up facts."""


def write_message(event: str, details: dict, now: datetime,
                  recent: list[str], use_search: bool) -> dict:
    """Ask Gemini (free) for a fresh title/body. Falls back to a template."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return fallback(event, details)

    # Out of AI time for this run: use a ready-written message
    if time.monotonic() - RUN_STARTED > AI_BUDGET_SECONDS:
        print("AI time budget used up, using fallback")
        return fallback(event, details)

    prompt = (
        f"Event: {event}\n"
        f"Now: {now:%A %d %B %Y, %H:%M}\n"
        f"Weekend: {'yes' if now.weekday() >= 5 else 'no'}\n"
        f"Details: {json.dumps(details, ensure_ascii=False)}\n"
        f"Recent notifications (do NOT repeat these):\n- "
        + "\n- ".join(recent[-12:] or ["(none)"])
        + "\n\nTipsdad's house style (match this tone and energy; write "
          "NEW wording, never copy these):\n"
        + messages.examples(event)
    )
    if use_search:
        prompt += ("\n\nUse Google Search once to check what's trending "
                   "in football today (big matches, derbies, cup finals, "
                   "tournaments, deadline day, major news) and use it "
                   "only if it fits naturally.")

    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 1.0, "maxOutputTokens": 400},
    }
    if use_search:
        # Free Google Search grounding for football trends
        payload["tools"] = [{"google_search": {}}]

    try:
        res = requests.post(
            GEMINI_URL.format(model=AI_MODEL),
            params={"key": api_key},
            json=payload,
            timeout=AI_TIMEOUT_SECONDS,
        )
        res.raise_for_status()
        parts = res.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        start, end = text.find("{"), text.rfind("}")
        msg = json.loads(text[start:end + 1])
        title = str(msg["title"]).strip()[:60]
        body = str(msg["body"]).strip()[:160]
        if title and body:
            return {"title": title, "body": body}
    except Exception as e:
        print(f"AI failed, using fallback: {e}")

    return fallback(event, details)


def fallback(event: str, d: dict) -> dict:
    """Ready-written messages: team names (allowed ones) or counts only.
    Uses the professional library in messages.py first."""
    msg = messages.render(event, d, datetime.now(TZ))
    if msg:
        return msg

    names = d.get("matches_you_may_name") or []
    first = names[0] if names else None
    n = d.get("matches") or d.get("total_matches") or ""

    if event == "tips_posted":
        options = [
            ("Today's tips are in ✅🏃‍➡️",
             f"{n} matches lined up today"
             + (f", including {first}." if first else ".")
             + " Open Tipsdad."),
            ("Fresh tips just landed",
             f"{n} matches ready for today on Tipsdad. Take a look."),
        ]
    elif event == "kickoff_soon":
        options = [
            ("Kickoff in minutes ⏱",
             (f"{first} and more kick off" if first else f"{n} match(es) kick off")
             + f" at {d.get('kickoff', '')}. Open Tipsdad."),
            ("Almost time!",
             f"{n} match(es) start at {d.get('kickoff', '')} on Tipsdad."),
        ]
    elif event == "won":
        options = [
            ("WON ✅🔥🔥 Just now!",
             (f"{first} came through" if first else
              f"{d.get('wins_now', '')} tip(s) just won")
             + ". Open Tipsdad to see it."),
            ("Another win ✅🔥🔥",
             f"{d.get('wins_now', '')} more tip(s) won. Don't miss the next ones!"),
        ]
    elif event == "vip_live":
        pkg = d.get("package", "VIP")
        options = [
            ("Your VIP match is live 🔴",
             (f"{first} is underway" if first else
              f"{n} {pkg} match(es) have kicked off")
             + f". Follow it on Tipsdad now."),
            (f"{pkg}: kickoff!",
             f"{n} of your {pkg} matches are live. Open Tipsdad."),
        ]
    elif event == "vip_won":
        pkg = d.get("package", "VIP")
        options = [
            (f"{pkg} WON ✅🔥🔥",
             (f"{first} came through for you" if first else
              f"{d.get('wins_now', '')} of your tips just won")
             + ". Open Tipsdad."),
            ("Your VIP tip won 🔥💸✅",
             f"{d.get('wins_now', '')} {pkg} tip(s) just won. Well done!"),
        ]
    elif event == "upsell":
        pkg = d.get("package", "another package")
        options = [
            (f"{pkg} just won ✅🎉💸🔥",
             "VIP Combined unlocks every VIP package. Upgrade on Tipsdad."),
            ("Don't miss the next one",
             f"{pkg} tips won today. Get them all with VIP Combined."),
        ]
    elif event == "resubscribe":
        options = [
            ("VIP keeps winning 🔥🎉💸✅",
             f"{d.get('vip_wins_today', '')} VIP tips won today. "
             "Renew your VIP on Tipsdad now."),
            ("We miss you at VIP",
             f"{d.get('vip_wins_today', '')} VIP wins today. "
             "Come back and renew your package."),
        ]
    elif event == "live_update":
        live_n = d.get("live_now", n)
        options = [
            ("Still live 🔴⚽",
             (f"{first} and {live_n - 1} more are on now"
              if first and live_n > 1 else
              f"{first} is still on" if first else
              f"{live_n} match(es) still live")
             + ". Follow them on Tipsdad 🔥"),
            ("Games in play 🔥",
             f"{live_n} match(es) live right now. Don't miss the finish!"),
        ]
    elif event == "results":
        options = [
            ("Results are in 📊",
             f"{d.get('won', 0)} won, {d.get('lost', 0)} lost. "
             "Next tips drop soon, stay ready!"),
            ("Today's outcome",
             f"{d.get('won', 0)} won and {d.get('lost', 0)} lost. "
             "Check History on Tipsdad now."),
        ]
    else:  # live
        options = [
            ("We're live 🔴",
             (f"{first} is underway" if first else f"{n} match(es) have kicked off")
             + ". Follow along on Tipsdad."),
            ("Matches underway",
             f"{n} match(es) are live now. Follow them on Tipsdad."),
        ]
    title, body = random.choice(options)
    return {"title": title, "body": body[:160]}


# ============================================================
# FCM
# ============================================================

def init_firebase() -> None:
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    if not raw:
        sys.exit("FIREBASE_SERVICE_ACCOUNT is not set")
    try:
        key = json.loads(raw.strip().lstrip("\ufeff"))
    except json.JSONDecodeError:
        sys.exit("FIREBASE_SERVICE_ACCOUNT is not valid JSON. Paste the "
                 "ENTIRE contents of the Firebase key file.")
    cred = credentials.Certificate(key)
    firebase_admin.initialize_app(cred)


def send(msg: dict, event: str, condition: str) -> None:
    if DRY_RUN:
        print(f"[DRY RUN] {event} → {condition}\n"
              f"          {msg['title']} | {msg['body']}")
        return
    message = messaging.Message(
        condition=condition,                     # who receives it
        notification=messaging.Notification(title=msg["title"],
                                            body=msg["body"]),
        data={"event": event},
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id=ANDROID_CHANNEL_ID,   # urgent pop-up channel
                sound="default",
                default_vibrate_timings=True,
            ),
        ),
    )
    print(f"Sent {event}: {messaging.send(message)} | {msg['title']}")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    now = datetime.now(TZ)
    state = load_state(now.strftime("%Y-%m-%d"))

    recent_tips = load_recent_tips(now)
    today_str = now.strftime("%Y-%m-%d")
    tips = [t for t in recent_tips if t["day"] == today_str]
    if not DRY_RUN:
        init_firebase()

    def can_send(audience: str = "all") -> bool:
        sent = state["sent_by_audience"].get(audience, 0)
        return sent < MAX_PER_DAY and now.hour not in QUIET_HOURS

    def do_send(event: str, key: str, details: dict,
                search: bool = False, condition: str = ALL_USERS,
                audience: str = "all") -> None:
        msg = write_message(event, details, now,
                            state["recent_messages"], search)
        send(msg, event, condition)
        state["sent_keys"].append(key)
        state["sent_today"] += 1
        state["sent_by_audience"][audience] = \
            state["sent_by_audience"].get(audience, 0) + 1
        state["recent_messages"] = (state["recent_messages"] +
                                    [f"{msg['title']} — {msg['body']}"])[-20:]
        # Save now, so a stopped run never sends this one again
        save_state(state)

    def by_package(items: list[dict]) -> dict[str, list[dict]]:
        """VIP tips grouped by package entitlement."""
        out: dict[str, list[dict]] = {}
        for t in items:
            if t["entitlement"]:
                out.setdefault(t["entitlement"], []).append(t)
        return out

    # ---------- 1. Tips posted ----------
    # Counts only, plus the names of today's FEATURED games
    ids = sorted(str(t["id"]) for t in tips)
    if ids != state["known_ids"]:
        new = set(ids) - set(state["known_ids"])
        state["known_ids"] = ids
        if new:
            state["last_change"] = now.isoformat()

    last_change = state["last_change"]
    upcoming = [t for t in tips if t["kickoff"] > now]
    if (upcoming and last_change and state["posted_sent"] < 2 and can_send()
            and now - datetime.fromisoformat(last_change)
            >= timedelta(minutes=POSTED_QUIET_MINUTES)
            and f"posted:{last_change}" not in state["sent_keys"]):
        featured_today = [t for t in tips if t["is_featured"]]
        first = min(t["kickoff"] for t in tips)
        do_send("tips_posted", f"posted:{last_change}", {
            "total_matches": len(tips),
            "free_matches": sum(1 for t in tips if not t["is_vip"]),
            "vip_matches": sum(1 for t in tips if t["is_vip"]),
            "featured_matches": len(featured_today),
            "first_kickoff": f"{first:%H:%M}",
            "leagues": sorted({t["league"] for t in tips if t["league"]})[:6],
            "matches_you_may_name": match_names(featured_today),
            "is_update": state["posted_sent"] > 0,
        }, search=True)
        state["posted_sent"] += 1

    # ---------- Group matches by kickoff time ----------
    groups: dict[datetime, list[dict]] = {}
    for t in tips:
        groups.setdefault(t["kickoff"], []).append(t)

    for kickoff, group in sorted(groups.items()):
        minutes = (kickoff - now).total_seconds() / 60
        group_featured = [t for t in group if t["is_featured"]]
        base = {
            "matches": len(group),
            "featured_matches": len(group_featured),
            "kickoff": f"{kickoff:%H:%M}",
            "leagues": sorted({t["league"] for t in group if t["league"]})[:4],
        }

        # ---------- 2. Kickoff soon ----------
        # Before kickoff: only FEATURED games may be named
        soon_key = f"soon:{kickoff:%H%M}"
        if 0 < minutes <= SOON_WINDOW_MINUTES and soon_key not in \
                state["sent_keys"] and can_send():
            do_send("kickoff_soon", soon_key, {
                **base,
                "minutes_to_kickoff": round(minutes),
                "matches_you_may_name": match_names(group_featured),
            })

        # ---------- 3. Live ----------
        # Once live: the live games may be named (featured first)
        live_key = f"live:{kickoff:%H%M}"
        if -LIVE_WINDOW_MINUTES <= minutes <= 0 and live_key not in \
                state["sent_keys"]:
            live_order = group_featured + [t for t in group
                                           if not t["is_featured"]]

            # a) Non-VIP users: general live alert
            if can_send("non_vip"):
                do_send("live", live_key, {
                    **base,
                    "matches_you_may_name": match_names(live_order),
                }, condition=NON_VIP, audience="non_vip")
            else:
                state["sent_keys"].append(live_key)

            vip_groups = by_package(group)

            # b) VIP Combined: one alert covering all their live matches
            vip_all = [t for t in live_order if t["entitlement"]]
            if vip_all and can_send("combined"):
                do_send("vip_live", f"{live_key}:{COMBINED}", {
                    **base,
                    "matches": len(vip_all),
                    "package": PACKAGE_NAME[COMBINED],
                    "matches_you_may_name": match_names(vip_all),
                }, condition=COMBINED_SUBS, audience="combined")

            # c) Each package's own subscribers: their live matches
            for ent, pkg_tips in vip_groups.items():
                if can_send(ent):
                    do_send("vip_live", f"{live_key}:{ent}", {
                        **base,
                        "matches": len(pkg_tips),
                        "package": PACKAGE_NAME[ent],
                        "matches_you_may_name": match_names(pkg_tips),
                    }, condition=package_subs(ent), audience=ent)

            # Don't send a late "kickoff soon" for the same group
            if soon_key not in state["sent_keys"]:
                state["sent_keys"].append(soon_key)

    # ---------- 4. Won (as soon as tips are marked Won) ----------
    # TODAY's tips only (yesterday's wins are never announced).
    # Several wins found in one check go out as ONE notification
    # per audience.
    new_wins = [t for t in tips
                if t["won"] and str(t["id"]) not in state["won_ids"]]
    if new_wins:
        # Remember these wins first, so a stopped run can't repeat them
        state["won_ids"] += [str(t["id"]) for t in new_wins]
        save_state(state)
        stamp = f"won:{now:%H%M}:{len(new_wins)}"
        featured_first = sorted(new_wins, key=lambda t: not t["is_featured"])

        # a) Non-VIP users: general win alert
        if (state["win_alerts_today"] < MAX_WIN_ALERTS_PER_DAY
                and can_send("non_vip")):
            do_send("won", stamp, {
                "wins_now": len(new_wins),
                "matches_you_may_name": match_names(featured_first),
                "wins_so_far_today": sum(1 for t in tips if t["won"]),
            }, condition=NON_VIP, audience="non_vip")
            state["win_alerts_today"] += 1

        vip_wins = [t for t in featured_first if t["entitlement"]]
        win_groups = by_package(vip_wins)

        # b) VIP Combined: one alert for all their wins
        if vip_wins and can_send("combined"):
            do_send("vip_won", f"{stamp}:{COMBINED}", {
                "wins_now": len(vip_wins),
                "package": PACKAGE_NAME[COMBINED],
                "matches_you_may_name": match_names(vip_wins),
            }, condition=COMBINED_SUBS, audience="combined")

        # c) Each package's subscribers: their wins
        for ent, pkg_wins in win_groups.items():
            if can_send(ent):
                do_send("vip_won", f"{stamp}:{ent}", {
                    "wins_now": len(pkg_wins),
                    "package": PACKAGE_NAME[ent],
                    "matches_you_may_name": match_names(pkg_wins),
                }, condition=package_subs(ent), audience=ent)

        # d) Upgrade to VIP Combined: users with ANOTHER package
        #    (once a day, for the package with the most new wins)
        if win_groups and state["upsell_today"] < MAX_UPSELL_PER_DAY \
                and can_send("upsell"):
            ent, pkg_wins = max(win_groups.items(), key=lambda kv: len(kv[1]))
            do_send("upsell", f"{stamp}:upsell:{ent}", {
                "package": PACKAGE_NAME[ent],
                "wins_now": len(pkg_wins),
                "matches_you_may_name": match_names(pkg_wins),
            }, condition=upsell_for(ent), audience="upsell")
            state["upsell_today"] += 1

        # e) Renew VIP: expired subscribers (at most every N days)
        last = state["last_lapsed"]
        due = (last is None or
               (now.date() - datetime.fromisoformat(last).date()).days
               >= LAPSED_EVERY_DAYS)
        vip_wins_today = sum(1 for t in tips if t["won"] and t["entitlement"])
        if vip_wins and due and vip_wins_today >= 2 and can_send("lapsed"):
            do_send("resubscribe", f"{stamp}:lapsed", {
                "vip_wins_today": vip_wins_today,
                "matches_you_may_name": match_names(vip_wins),
            }, condition=LAPSED, audience="lapsed")
            state["last_lapsed"] = now.isoformat()


    # ---------- 4b. Live update every 45 minutes ----------
    # Any match kicked off in the last ~110 min and not settled yet
    in_play = [t for t in tips
               if t["kickoff"] <= now
               < t["kickoff"] + timedelta(minutes=MATCH_LENGTH_MINUTES)
               and not t["settled"]]
    last_upd = state["last_live_update"]
    upd_due = (last_upd is None or now - datetime.fromisoformat(last_upd)
               >= timedelta(minutes=LIVE_UPDATE_EVERY_MINUTES))
    if (in_play and upd_due
            and state["live_updates_today"] < MAX_LIVE_UPDATES_PER_DAY
            and now.hour not in QUIET_HOURS):
        # Skip if a kickoff "live" alert just went out in this run
        just_live = any(f"live:{t['kickoff']:%H%M}" in state["sent_keys"]
                        and now - t["kickoff"] < timedelta(minutes=10)
                        for t in in_play)
        if not just_live:
            order = sorted(in_play, key=lambda t: not t["is_featured"])
            do_send("live_update", f"live_update:{now:%H%M}", {
                "live_now": len(in_play),
                "leagues": sorted({t["league"] for t in in_play
                                   if t["league"]})[:4],
                "matches_you_may_name": match_names(order),
            }, audience="live_update")
        # Start the 45-minute clock either way
        state["last_live_update"] = now.isoformat()
        state["live_updates_today"] += 1 if not just_live else 0

    # ---------- 5. Results summary (after ALL tips are settled) ----------
    days: dict[str, list[dict]] = {}
    for t in recent_tips:
        days.setdefault(t["day"], []).append(t)

    for day, day_tips in sorted(days.items()):
        # Track when statuses last changed for this day
        for t in day_tips:
            key = str(t["id"])
            status = "won" if t["won"] else "lost" if t["lost"] else \
                "settled" if t["settled"] else "pending"
            if state["settled_seen"].get(key) != status:
                state["settled_seen"][key] = status
                state["settle_change"][day] = now.isoformat()

        if day in state["summaries"]:
            continue
        if not all(t["settled"] for t in day_tips):
            continue   # history not fully updated yet

        changed = state["settle_change"].get(day)
        if changed and now - datetime.fromisoformat(changed) < \
                timedelta(minutes=RESULTS_QUIET_MINUTES):
            continue   # still being updated, wait a bit

        if not can_send():
            continue

        won = sum(1 for t in day_tips if t["won"])
        lost = sum(1 for t in day_tips if t["lost"])
        do_send("results", f"results:{day}", {
            "day": "today" if day == today_str else "yesterday",
            "won": won,
            "lost": lost,
            "void_or_postponed": len(day_tips) - won - lost,
            "win_rate_percent": round(won / (won + lost) * 100)
            if won + lost else None,
            "matches_you_may_name": match_names(
                sorted([t for t in day_tips if t["won"]],
                       key=lambda t: not t["is_featured"])),
        })
        state["summaries"].append(day)

    # Forget statuses of tips older than yesterday
    live_ids = {str(t["id"]) for t in recent_tips}
    state["settled_seen"] = {k: v for k, v in state["settled_seen"].items()
                             if k in live_ids}
    state["settle_change"] = {k: v for k, v in state["settle_change"].items()
                              if k in days}

    save_state(state)

    # ---------- Status report (shows in the GitHub Actions log) ----------
    upcoming_k = sorted({t["kickoff"] for t in tips if t["kickoff"] > now})
    print(f"Now (Nairobi): {now:%a %d %b %H:%M}"
          f"{'  [QUIET HOURS: nothing is sent]' if now.hour in QUIET_HOURS else ''}")
    print(f"Tips from API: {len(recent_tips)} today+yesterday, "
          f"{len(tips)} today, {len(in_play)} in play now, "
          f"{len(upcoming_k)} kickoff times still ahead")
    if upcoming_k:
        print("Next kickoffs: " + ", ".join(f"{k:%H:%M}" for k in upcoming_k[:6]))
    if state["last_live_update"]:
        print(f"Last live update: {state['last_live_update'][11:16]} "
              f"(next one {LIVE_UPDATE_EVERY_MINUTES} min later, if games are on)")
    print(f"Sent today: {state['sent_today']}  "
          f"(by audience: {state['sent_by_audience'] or '-'})")
    print(f"Done. {len(tips)} tips today, {state['sent_today']} sent today.")


if __name__ == "__main__":
    main()