"""
Tipsdad automatic push notifications.

Run every 5 minutes (GitHub Actions or a cPanel cron). Each run:
  1. Loads today's tips from the Tipsdad API.
  2. Cycles of 3 notifications, 5 minutes apart, then a 45-minute break:
       0   "won" (tips that WON since the last cycle) or "confidence" (real record)
       +5  "live_update" / "kickoff_soon" / "next_kickoff"
       +10 an AI football fact about one of today's games (notify_loop.py)
     then a 45-minute break after the 3rd, and the cycle repeats.
     Plus two once-a-day messages, sent when they happen:
       • "tips posted"  – once today's tips stop changing for a while
       • "results"      – once ALL of a day's tips are settled: won/lost
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
import re
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
# Tried in order. If Google says a model is full (429) or retired (404),
# the next one is tried, plus any model Google suggests in its answer.
AI_MODELS = [AI_MODEL] + [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS",
    "gemini-3.8-flash,gemini-flash-lite-latest,gemini-3.8-flash-lite",
).split(",") if m.strip() and m.strip() != AI_MODEL]
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")
_good_model = None          # the model that worked last in this run


class GeminiError(RuntimeError):
    def __init__(self, status: int, text: str):
        super().__init__(f"{status}: {text}")
        self.status = status


def gemini_error(res) -> str:
    """Google's own explanation of an error, in one short line."""
    try:
        return str(res.json()["error"]["message"]).split("\n")[0][:220]
    except Exception:
        return res.text[:220]


def gemini_post(payload: dict, api_key: str, timeout: int,
                log: str = "AI: "):
    """Send to Gemini, moving on to the next model if one is full or gone."""
    global _good_model
    queue = ([_good_model] if _good_model else []) + list(AI_MODELS)
    tried, last = set(), None
    while queue:
        model = queue.pop(0)
        if model in tried:
            continue
        tried.add(model)
        res = requests.post(GEMINI_URL.format(model=model),
                            headers={"x-goog-api-key": api_key},
                            json=payload, timeout=timeout)
        if res.ok:
            _good_model = model
            return res
        why = gemini_error(res)
        last = GeminiError(res.status_code, why)
        if res.status_code not in (404, 429):
            break
        print(f"{log}{model} not usable ({res.status_code}): {why}")
        # "...please use models/gemini-X-flash..." -> try that one next
        hint = re.search(r"use (?:models/)?(gemini-[\w.\-]*\w)", why)
        if hint and hint.group(1) not in tried:
            queue.insert(0, hint.group(1))
    raise last or GeminiError(0, "no Gemini model available")

POSTED_QUIET_MINUTES = 20   # "tips posted" once no new tips for this long
SOON_WINDOW_MINUTES = 12    # "kickoff soon" when a match starts within this
LIVE_WINDOW_MINUTES = 20    # "live" up to this long after kickoff
MAX_PER_DAY = int(os.getenv("MAX_PER_DAY", "30"))  # cap per audience per day
QUIET_HOURS = range(1, 7)   # no notifications 01:00–06:59 Nairobi time

# The cycle: 3 notifications 5 minutes apart, then a 45-minute break.
#   0 min   1st: results update (wins since last cycle, else today's record)
#   +5 min  2nd: live update    (live now / kicking off soon / next kickoff)
#   +10 min 3rd: football fact  (AI, in notify_loop.py)
#   then wait 45 minutes after the 3rd, and start again.
PULSE_EVERY_MINUTES = int(os.getenv("PULSE_EVERY_MINUTES", "45"))   # the break
SLOT_SPACING_MINUTES = int(os.getenv("SLOT_SPACING_MINUTES", "5"))
LIVE_SLOT_MINUTES = SLOT_SPACING_MINUTES
FACT_SLOT_MINUTES = 2 * SLOT_SPACING_MINUTES
CYCLE_MINUTES = FACT_SLOT_MINUTES + PULSE_EVERY_MINUTES             # 55
PULSE_SLACK_MINUTES = 1          # a run can arrive a few seconds early
MATCH_LENGTH_MINUTES = 110       # a match counts as in play this long
MAX_CONFIDENCE_PER_DAY = 30      # "here's our record" when no new wins this cycle
SEND_KICKOFF_ALERTS = False      # True = ALSO send "kickoff soon"/"live" right at kickoff

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
# Off: the normal notifications use the ready-written library, and Gemini is
# kept for the daily football facts only. AI_WORDING=1 turns AI wording on.
AI_WORDING = os.getenv("AI_WORDING", "0") == "1"
_ai_rate_limited = False   # set after a 429; no more AI wording this run


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
             "last_lapsed", "last_pulse", "last_results_at", "cycle",
             "fact_history")
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
    state.setdefault("last_pulse", None)     # time of the last 45-min pulse
    state.setdefault("confidence_today", 0)

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
- "won", "vip_won", "live_update" and "vip_live" may include "record_today"
  (won/lost so far) and "starting_soon": mention them briefly when they help.
- "next_kickoff" is a countdown to today's next match when nothing is live:
  use "kickoff" and "wait" (e.g. "first whistle in 2h 15m"), counts only.
- "confidence" is sent when nothing new happened in the last 45 minutes:
  build trust with the REAL record in the details (today so far, yesterday,
  current win streak, matches still to play). Use the exact numbers. If
  losses outnumber wins, don't hide them: talk about transparency instead
  ("every result is in History") and the next matches. Never promise wins.
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


def _examples(event: str) -> str:
    try:
        return messages.examples(event) or "(none)"
    except Exception:
        return "(none)"


def write_message(event: str, details: dict, now: datetime,
                  recent: list[str], use_search: bool) -> dict:
    """Ask Gemini (free) for a fresh title/body. Falls back to a template."""
    global _ai_rate_limited
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not AI_WORDING or _ai_rate_limited:
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
        + _examples(event)
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
        res = gemini_post(payload, api_key, AI_TIMEOUT_SECONDS)
        parts = res.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        start, end = text.find("{"), text.rfind("}")
        msg = json.loads(text[start:end + 1])
        title = str(msg["title"]).strip()[:60]
        body = str(msg["body"]).strip()[:160]
        if title and body:
            return {"title": title, "body": body}
    except Exception as e:
        if getattr(e, "status", None) == 429:
            # Free quota hit: stop asking for the rest of this run, so the
            # other messages go out straight away and the fact slot keeps
            # its share of the quota
            _ai_rate_limited = True
        print(f"AI failed, using fallback: {str(e).replace(api_key, '***')}")

    return fallback(event, details)


def fallback(event: str, d: dict) -> dict:
    """Ready-written messages: team names (allowed ones) or counts only.
    Uses the professional library in messages.py first."""
    try:
        msg = messages.render(event, d, datetime.now(TZ))
    except Exception:
        msg = None
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
        live_n = d.get("live_now", n) or n or 1
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
    elif event == "next_kickoff":
        options = [
            ("Next kickoff ⏱",
             (f"{first} and more kick off" if first else f"{n} match(es) kick off")
             + f" at {d.get('kickoff', '')}. Open Tipsdad."),
            ("Coming up ⚽",
             f"{n} match(es) start in {d.get('wait', '')} "
             f"({d.get('kickoff', '')}). Get ready on Tipsdad."),
        ]
    elif event == "engagement":
        options = [
            ("Today on Tipsdad ⚽",
             f"{n} matches on today's list. Open Tipsdad."),
            ("Have you checked today's tips?",
             f"{n} matches lined up, first kickoff {d.get('kickoff', '')}."),
        ]
    elif event == "confidence":
        tw, tl = d.get("today_won", 0), d.get("today_lost", 0)
        yw, yl = d.get("yesterday_won", 0), d.get("yesterday_lost", 0)
        streak = d.get("win_streak", 0)
        left = d.get("matches_still_to_play", 0)
        options = []
        if tw + tl:
            options.append(("Today so far 📊",
                             f"{tw} won, {tl} lost"
                             + (f", {left} still to play" if left else "")
                             + ". Every result is in History on Tipsdad."))
        if yw + yl:
            options.append(("Yesterday's record ✅",
                            f"{yw} won, {yl} lost. All results are public in History."))
        if streak >= 2:
            options.append((f"{streak} wins in a row 🔥",
                            "Our last results are all green. Check History on Tipsdad."))
        if left:
            options.append(("More matches ahead ⚽",
                            f"{left} matches still to play today. Stay with Tipsdad."))
        if not options:
            options = [("Stay ready ⚽", "New tips drop soon on Tipsdad. Turn notifications on.")]
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
# 45-MINUTE PULSE
# ============================================================

def in_play_now(tips: list[dict], now: datetime) -> list[dict]:
    """Kicked off less than MATCH_LENGTH_MINUTES ago and not settled yet."""
    return [t for t in tips
            if t["kickoff"] <= now
            < t["kickoff"] + timedelta(minutes=MATCH_LENGTH_MINUTES)
            and not t["settled"]]


def record_of(items: list[dict]) -> dict:
    won = sum(1 for t in items if t["won"])
    lost = sum(1 for t in items if t["lost"])
    return {"won": won, "lost": lost}


def win_streak(recent_tips: list[dict]) -> int:
    """Wins in a row, newest settled tip first (void/postponed are skipped)."""
    streak = 0
    for t in sorted(recent_tips, key=lambda t: t["kickoff"], reverse=True):
        if t["won"]:
            streak += 1
        elif t["lost"]:
            break
    return streak


def summaries_ready(now, recent_tips, state) -> list[tuple[str, list[dict]]]:
    """Days whose tips are ALL settled, unchanged for a while, not summarised yet."""
    days: dict[str, list[dict]] = {}
    for t in recent_tips:
        days.setdefault(t["day"], []).append(t)
    ready = []
    for day, day_tips in sorted(days.items()):
        if day in state["summaries"] or not all(t["settled"] for t in day_tips):
            continue
        changed = state["settle_change"].get(day)
        if changed and now - datetime.fromisoformat(changed) < \
                timedelta(minutes=RESULTS_QUIET_MINUTES):
            continue   # still being updated, wait a bit
        ready.append((day, day_tips))
    return ready


def slot_results(now, tips, recent_tips, state, can_send, do_send,
                 by_package) -> None:
    """1st of the cycle: the day's results summary if the day just finished;
    else wins since the last cycle; else today's real record."""
    stamp = f"cycle:{now:%Y%m%d%H%M}"
    today_str = now.strftime("%Y-%m-%d")

    # ---- a) A day just finished: results summary (+ VIP members' own wins)
    ready = summaries_ready(now, recent_tips, state)
    if ready and can_send():
        for day, day_tips in ready:
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
            state["last_results_at"] = now.isoformat()

            # Wins not announced yet: the summary covers everyone, and VIP
            # members also get THEIR package's win in this same slot.
            fresh = [t for t in day_tips
                     if t["won"] and str(t["id"]) not in state["won_ids"]]
            state["won_ids"] += [str(t["id"]) for t in fresh]
            vip_fresh = sorted([t for t in fresh if t["entitlement"]],
                               key=lambda t: not t["is_featured"])
            if vip_fresh and can_send("combined"):
                do_send("vip_won", f"{stamp}:won:{COMBINED}", {
                    "wins_now": len(vip_fresh),
                    "package": PACKAGE_NAME[COMBINED],
                    "matches_you_may_name": match_names(vip_fresh),
                }, condition=COMBINED_SUBS, audience="combined")
            for ent, pkg_wins in by_package(vip_fresh).items():
                if can_send(ent):
                    do_send("vip_won", f"{stamp}:won:{ent}", {
                        "wins_now": len(pkg_wins),
                        "package": PACKAGE_NAME[ent],
                        "matches_you_may_name": match_names(pkg_wins),
                    }, condition=package_subs(ent), audience=ent)
        save_state(state)
        return
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    in_play = in_play_now(tips, now)
    today_rec = record_of(tips)

    new_wins = [t for t in tips
                if t["won"] and str(t["id"]) not in state["won_ids"]]
    if new_wins:
        state["won_ids"] += [str(t["id"]) for t in new_wins]
        save_state(state)
        featured_first = sorted(new_wins, key=lambda t: not t["is_featured"])

        if (state["win_alerts_today"] < MAX_WIN_ALERTS_PER_DAY
                and can_send("non_vip")):
            do_send("won", f"{stamp}:won", {
                "wins_now": len(new_wins),
                "matches_you_may_name": match_names(featured_first),
                "wins_so_far_today": today_rec["won"],
                "record_today": today_rec,
                "live_now": len(in_play),
            }, condition=NON_VIP, audience="non_vip")
            state["win_alerts_today"] += 1

        vip_wins = [t for t in featured_first if t["entitlement"]]
        win_groups = by_package(vip_wins)

        if vip_wins and can_send("combined"):
            do_send("vip_won", f"{stamp}:won:{COMBINED}", {
                "wins_now": len(vip_wins),
                "package": PACKAGE_NAME[COMBINED],
                "matches_you_may_name": match_names(vip_wins),
                "record_today": record_of([t for t in tips if t["entitlement"]]),
            }, condition=COMBINED_SUBS, audience="combined")

        for ent, pkg_wins in win_groups.items():
            if can_send(ent):
                do_send("vip_won", f"{stamp}:won:{ent}", {
                    "wins_now": len(pkg_wins),
                    "package": PACKAGE_NAME[ent],
                    "matches_you_may_name": match_names(pkg_wins),
                    "record_today": record_of(
                        [t for t in tips if t["entitlement"] == ent]),
                }, condition=package_subs(ent), audience=ent)

        # Upgrade to VIP Combined (once a day)
        if win_groups and state["upsell_today"] < MAX_UPSELL_PER_DAY \
                and can_send("upsell"):
            ent, pkg_wins = max(win_groups.items(), key=lambda kv: len(kv[1]))
            do_send("upsell", f"{stamp}:upsell:{ent}", {
                "package": PACKAGE_NAME[ent],
                "wins_now": len(pkg_wins),
                "matches_you_may_name": match_names(pkg_wins),
            }, condition=upsell_for(ent), audience="upsell")
            state["upsell_today"] += 1

        # Renew VIP (expired subscribers, at most every N days)
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
        return

    # No new wins: build confidence with the real record
    last_res = state.get("last_results_at")
    if last_res and now - datetime.fromisoformat(last_res) \
            < timedelta(minutes=PULSE_EVERY_MINUTES):
        return   # the results summary just went out
    if state["confidence_today"] >= MAX_CONFIDENCE_PER_DAY \
            or not can_send("confidence"):
        return
    y_rec = record_of([t for t in recent_tips if t["day"] == yesterday])
    still_to_play = sum(1 for t in tips if t["kickoff"] > now)
    if today_rec["won"] + today_rec["lost"] + y_rec["won"] + y_rec["lost"] == 0:
        return   # no record yet: the live slot covers today's matches
    do_send("confidence", f"{stamp}:confidence", {
        "today_won": today_rec["won"],
        "today_lost": today_rec["lost"],
        "yesterday_won": y_rec["won"],
        "yesterday_lost": y_rec["lost"],
        "win_streak": win_streak(recent_tips),
        "matches_still_to_play": still_to_play,
        # only winning, already-finished matches may be named
        "matches_you_may_name": match_names(sorted(
            [t for t in tips if t["won"]], key=lambda t: not t["is_featured"])),
    }, audience="confidence")
    state["confidence_today"] += 1


def slot_live(now, tips, state, can_send, do_send, by_package) -> None:
    """+15 min: matches live now; else kicking off before the next cycle;
    else the next kickoff later today."""
    stamp = f"cycle:{now:%Y%m%d%H%M}"
    in_play = in_play_now(tips, now)
    soon = [t for t in tips if now < t["kickoff"]
            <= now + timedelta(minutes=CYCLE_MINUTES)]
    later = sorted((t for t in tips if t["kickoff"] > now),
                   key=lambda t: t["kickoff"])

    def send_for(items, audience, condition, live_event, package=None) -> bool:
        if not can_send(audience):
            return False
        extra = {"package": package} if package else {}
        live = [t for t in in_play if t in items]
        nxt = [t for t in soon if t in items]
        if live:
            order = sorted(live, key=lambda t: not t["is_featured"])
            do_send(live_event, f"{stamp}:live:{audience}", {
                **extra,
                "live_now": len(live),
                "matches": len(live),
                "starting_soon": len(nxt),
                "leagues": sorted({t["league"] for t in live if t["league"]})[:4],
                "matches_you_may_name": match_names(order),   # live games
                "record_today": record_of(items),
            }, condition=condition, audience=audience)
            return True
        if nxt:
            first = min(t["kickoff"] for t in nxt)
            do_send("kickoff_soon", f"{stamp}:soon:{audience}", {
                **extra,
                "matches": len(nxt),
                "kickoff": f"{first:%H:%M}",
                "minutes_to_kickoff": max(1, round((first - now).total_seconds() / 60)),
                "leagues": sorted({t["league"] for t in nxt if t["league"]})[:4],
                # before kickoff only FEATURED games may be named
                "matches_you_may_name": match_names(
                    [t for t in nxt if t["is_featured"]]),
            }, condition=condition, audience=audience)
            return True
        return False

    told = False
    if in_play or soon:
        told |= send_for(tips, "non_vip", NON_VIP, "live_update")
        vip_today = [t for t in tips if t["entitlement"]]
        if vip_today:
            told |= send_for(vip_today, "combined", COMBINED_SUBS, "vip_live",
                             PACKAGE_NAME[COMBINED])
        for ent, pkg_tips in by_package(vip_today).items():
            told |= send_for(pkg_tips, ent, package_subs(ent), "vip_live",
                             PACKAGE_NAME[ent])

    # Nothing live or about to start: countdown to the next kickoff today
    if not told and later and can_send("all"):
        nxt = later[0]["kickoff"]
        group = [t for t in later if t["kickoff"] == nxt]
        mins = max(1, round((nxt - now).total_seconds() / 60))
        hours, rest = divmod(mins, 60)
        do_send("next_kickoff", f"{stamp}:next", {
            "matches": len(group),
            "kickoff": f"{nxt:%H:%M}",
            "minutes_to_kickoff": mins,
            "wait": f"{hours}h {rest}m" if hours else f"{rest} min",
            "matches_still_ahead_today": len(later),
            "leagues": sorted({t["league"] for t in group if t["league"]})[:4],
            "matches_you_may_name": match_names(
                [t for t in group if t["is_featured"]]),
        })


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

    for kickoff, group in (sorted(groups.items()) if SEND_KICKOFF_ALERTS else []):
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

    # ---------- Track result changes (the summary is sent in the cycle) ----------
    days: dict[str, list[dict]] = {}
    for t in recent_tips:
        days.setdefault(t["day"], []).append(t)
    for day, day_tips in days.items():
        for t in day_tips:
            key = str(t["id"])
            status = "won" if t["won"] else "lost" if t["lost"] else \
                "settled" if t["settled"] else "pending"
            if state["settled_seen"].get(key) != status:
                state["settled_seen"][key] = status
                state["settle_change"][day] = now.isoformat()

    # ---------- 4. The cycle: 1st at 0, 2nd at +5 (3rd at +10 is notify_loop.py) ----------
    if now.hour not in QUIET_HOURS:
        cyc = state.get("cycle") or {}
        start = datetime.fromisoformat(cyc["at"]) if cyc.get("at") else None
        ended = datetime.fromisoformat(cyc["ended"]) if cyc.get("ended") else None
        stale = start is not None and now - start > timedelta(
            minutes=CYCLE_MINUTES + 30)      # e.g. interrupted by quiet hours
        if (start is None or stale or (ended is not None and now - ended
                >= timedelta(minutes=PULSE_EVERY_MINUTES - PULSE_SLACK_MINUTES))):
            start, cyc = now, {"at": now.isoformat(), "done": []}
        mins = (now - start).total_seconds() / 60
        state["cycle"] = cyc

        def filler(slot: str) -> None:
            """A slot had nothing new: send a banter / free-tips line instead,
            so every cycle still delivers its notification."""
            if not tips or not can_send("all"):
                return
            msg = messages.extras({
                "matches": len(tips),
                "kickoff": f"{min(t['kickoff'] for t in tips):%H:%M}",
            })
            if not msg:
                return
            send(msg, "banter", ALL_USERS)
            state["sent_keys"].append(f"cycle:{now:%Y%m%d%H%M}:{slot}:filler")
            state["sent_today"] += 1
            state["sent_by_audience"]["all"] = \
                state["sent_by_audience"].get("all", 0) + 1
            state["recent_messages"] = (state["recent_messages"] +
                                        [f"{msg['title']} — {msg['body']}"])[-20:]
            save_state(state)

        if "results" not in cyc["done"]:
            cyc["done"].append("results")
            save_state(state)
            before = state["sent_today"]
            slot_results(now, tips, recent_tips, state, can_send, do_send,
                         by_package)
            if state["sent_today"] == before:
                filler("results")
        if "live" not in cyc["done"] and \
                mins >= LIVE_SLOT_MINUTES - PULSE_SLACK_MINUTES:
            cyc["done"].append("live")
            save_state(state)
            before = state["sent_today"]
            slot_live(now, tips, state, can_send, do_send, by_package)
            if state["sent_today"] == before:
                filler("live")

    # Forget statuses of tips older than yesterday
    live_ids = {str(t["id"]) for t in recent_tips}
    state["settled_seen"] = {k: v for k, v in state["settled_seen"].items()
                             if k in live_ids}
    state["settle_change"] = {k: v for k, v in state["settle_change"].items()
                              if k in days}

    save_state(state)

    # ---------- Status report (shows in the GitHub Actions log) ----------
    in_play = in_play_now(tips, now)
    upcoming_k = sorted({t["kickoff"] for t in tips if t["kickoff"] > now})
    print(f"Now (Nairobi): {now:%a %d %b %H:%M}"
          f"{'  [QUIET HOURS: nothing is sent]' if now.hour in QUIET_HOURS else ''}")
    print(f"Tips from API: {len(recent_tips)} today+yesterday, "
          f"{len(tips)} today, {len(in_play)} in play now, "
          f"{len(upcoming_k)} kickoff times still ahead")
    if upcoming_k:
        print("Next kickoffs: " + ", ".join(f"{k:%H:%M}" for k in upcoming_k[:6]))
    cyc = state.get("cycle") or {}
    if cyc.get("at"):
        st = datetime.fromisoformat(cyc["at"])
        nxt = (datetime.fromisoformat(cyc["ended"]) if cyc.get("ended")
               else st + timedelta(minutes=FACT_SLOT_MINUTES)) + \
            timedelta(minutes=PULSE_EVERY_MINUTES)
        print(f"Cycle started {st:%H:%M}, done: {', '.join(cyc['done']) or '-'}; "
              f"next cycle ~{nxt:%H:%M}")
    print(f"Sent today: {state['sent_today']}  "
          f"(by audience: {state['sent_by_audience'] or '-'})")
    print(f"Done. {len(tips)} tips today, {state['sent_today']} sent today.")


if __name__ == "__main__":
    main()