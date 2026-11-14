"""
Tipsdad notification library.

Professionally written push notifications, grouped by the bot's events.
Each one is a TEMPLATE filled with REAL data from the tips API, so no
result, fixture or record is ever invented, and no tip, odds or
guarantee is ever shown.

Placeholders (a template is only used when all of its data exists):
  {match}   first team pairing the bot may name, e.g. "Arsenal vs Chelsea"
  {match2}  second allowed pairing
  {n}       number of matches
  {kickoff} kickoff time, e.g. "21:00"
  {mins}    minutes to kickoff
  {ahead}   matches still to play today
  {wins}    number of tips that just won
  {won}     tips won (results)
  {lost}    tips lost (results)
  {pkg}     VIP package name, e.g. "VIP GG"
  {comp}    big competition in today's list, e.g. "Champions League"
  {tw} {tl} today's record so far (won / lost)
  {yw} {yl} yesterday's record (won / lost)
  {streak}  wins in a row (only used when 2 or more)

Used by:
  • notify.py      ready-written backup when Gemini is unavailable,
                   and style examples shown to Gemini
  • notify_loop.py banter / free-tips line when the AI has no confirmed
                   football fact for the +30 min slot of a 45-minute cycle
"""

import random
import string
from datetime import datetime

# ============================================================
# EVENT MESSAGES
# ============================================================

LIBRARY: dict[str, list[tuple[str, str]]] = {

    # ---------- Daily predictions / new tips posted ----------
    "tips_posted": [
        ("Today's slate just dropped ⚽",
         "{n} matches analysed and ready. Open Tipsdad and see where the value is today."),
        ("🚨 Fresh tips are live",
         "The team has done the homework on {n} matches. Your move: open Tipsdad."),
        ("We've been busy 👀",
         "{n} new predictions just landed, including {match}. Take a look before kickoff."),
        ("Your matchday briefing is ready",
         "{n} games, one app. Today's analysis is waiting for you on Tipsdad."),
        ("New tips. Same hunger. 🔥",
         "{n} matches broken down for today. First kickoff at {kickoff}. Don't sleep on it."),
        ("{match} is on today's list",
         "One of {n} matches analysed today. See what Tipsdad is thinking before kickoff."),
        ("The board is set ♟️",
         "{n} matches, fresh analysis, first whistle at {kickoff}. Open Tipsdad now."),
        ("Read this before {kickoff} ⏰",
         "Today's {n} predictions are up. The early birds already took a look."),
        ("Tips are in. Coffee optional ☕",
         "{n} matches ready for you on Tipsdad. Start your football day here."),
        ("Something caught our eye today 👀",
         "{match} headlines {n} new predictions. Open Tipsdad to see why."),
        ("Today's picks are out 📋",
         "Free and VIP predictions for {n} matches are live. Find yours on Tipsdad."),
    ],

    # ---------- Countdown / last-minute reminders ----------
    "kickoff_soon": [
        ("⏳ {mins} minutes to kickoff",
         "{n} matches start at {kickoff}. Last chance to check Tipsdad before the whistle."),
        ("Tunnel walk time 🚶‍♂️",
         "Players are lining up. {n} games kick off at {kickoff}. Are you ready?"),
        ("Clock's ticking ⏱️",
         "{match} and more start in {mins} minutes. Open Tipsdad now."),
        ("Final call before kickoff 📣",
         "{n} matches go at {kickoff}. Check today's analysis while there's still time."),
        ("Don't be the one who missed it",
         "Kickoff at {kickoff}. Your Tipsdad predictions are one tap away."),
        ("Boots on. Whistle soon. ⚽",
         "{mins} minutes until {n} matches begin. See the predictions now."),
        ("{match} kicks off at {kickoff}",
         "Minutes left. Open Tipsdad and get the full picture before it starts."),
        ("The countdown is real 🔥",
         "{n} games, {mins} minutes away. Your last look starts now."),
    ],

    # ---------- Later today (kickoff more than 30 minutes away) ----------
    "next_kickoff": [
        ("Tonight's first whistle: {kickoff} ⚽",
         "{ahead} matches still to come today. The analysis is ready on Tipsdad."),
        ("Kickoff in {wait} ⏳",
         "Plenty of time to read today's predictions. {ahead} matches ahead."),
        ("Mark your calendar 📅",
         "Next kickoff {kickoff}. Today's Tipsdad analysis is waiting for you."),
        ("Matchday warm-up 🏃‍♂️",
         "{ahead} games still to play today. Get ahead of them on Tipsdad."),
        ("Plan your evening ⚽",
         "First game at {kickoff}, {ahead} on the list. Open Tipsdad."),
        ("The build-up starts now 🔥",
         "Kickoff in {wait}. See what Tipsdad expects from today's matches."),
    ],

    # ---------- Breaking: matches just kicked off ----------
    "live": [
        ("🚨 We're LIVE",
         "{match} has kicked off. Follow the action on Tipsdad right now."),
        ("Kickoff! 💥",
         "{n} matches are underway. Live from Tipsdad: every minute counts."),
        ("And they're off ⚽",
         "{match} and {match2} just started. Open Tipsdad and follow along."),
        ("The whistle has gone 🔔",
         "{n} games running now. Tipsdad has the live picture."),
        ("It's happening 🔥",
         "{match} is live. Your matchday just got interesting."),
        ("Live and loud 📢",
         "{n} matches just kicked off. Grab your seat on Tipsdad."),
        ("Game on 🎮⚽",
         "{match} is underway. Don't follow it blind: open Tipsdad."),
        ("First whistle. Full focus. 👀",
         "{n} matches live right now. Track them on Tipsdad."),
    ],

    # ---------- Live updates while matches are in play ----------
    "live_update": [
        ("Still on. Still tense. 😬",
         "{match} is still in play. Check how it's going on Tipsdad."),
        ("🔴 Live right now",
         "{n} matches still running. One tap and you're in the action."),
        ("Second half energy ⚡",
         "{match} is still being played. Don't miss how this ends."),
        ("Nerves? Same. 😅",
         "{n} games still live. Follow every twist on Tipsdad."),
        ("The clock is running ⏱️",
         "{match} is still going. Open Tipsdad for the latest."),
        ("Anything can happen now 👀",
         "{n} matches in play. The finish could change everything."),
        ("Late drama loading… 🎬",
         "{match} isn't over yet. Stay close on Tipsdad."),
        ("Pulse check 💓",
         "{n} matches live and counting. How are yours going?"),
    ],

    # ---------- Winning moments ----------
    "won": [
        ("✅ That's a WIN",
         "{match} just landed. See it in Tipsdad History."),
        ("Called it 🎯",
         "{match} came through. Open Tipsdad and see what's next."),
        ("Green tick alert ✅",
         "{wins} tip(s) just won. The scoreboard is moving on Tipsdad."),
        ("Boom 💥",
         "{match}: in the bag. Check today's record on Tipsdad."),
        ("Another one ✅🔥",
         "{wins} more tip(s) won just now. Don't miss what's coming next."),
        ("Full-time smiles 😁",
         "{match} delivered. See the result in Tipsdad History."),
        ("The analysis spoke ✅",
         "{match} finished on the right side. Open Tipsdad for the next ones."),
        ("Winner, winner ⚽",
         "{wins} tip(s) just came good. Open Tipsdad and see them."),
    ],

    # ---------- Results / performance (always the real numbers) ----------
    "results": [
        ("📊 The day in numbers",
         "{won} won, {lost} lost. Full breakdown in Tipsdad History. Next tips soon."),
        ("Results are in",
         "Today: {won} won and {lost} lost. Honest record, every day. See it on Tipsdad."),
        ("Scoreboard update 📈",
         "{won} wins and {lost} losses today. Tomorrow's analysis is already underway."),
        ("Full-time report 📝",
         "{won} won, {lost} lost. Every result is posted in Tipsdad History."),
        ("How did we do? 👀",
         "{won} won, {lost} lost today. Open Tipsdad and check every match."),
        ("That's a wrap 🎬",
         "Today closed {won}-{lost}. Rest up: new predictions drop soon."),
    ],

    # ---------- Confidence: the real record (no new wins this cycle) ----------
    "confidence": [
        ("📊 Today so far: {tw} won",
         "{tw} won, {tl} lost and {ahead} still to play. Every result is in Tipsdad History."),
        ("Honest numbers, every day 📈",
         "Today: {tw} won, {tl} lost. Check every match yourself in History."),
        ("Yesterday went {yw}-{yl} ✅",
         "Every result, won or lost, is posted in Tipsdad History. Today's list is live."),
        ("{streak} in a row 🔥",
         "Our last {streak} settled tips all won. See them in Tipsdad History."),
        ("Scoreboard check 📋",
         "Today {tw}-{tl} so far, {ahead} matches still to come. Stay with Tipsdad."),
        ("Don't take our word for it 👀",
         "Yesterday: {yw} won, {yl} lost. The full record is in Tipsdad History."),
        ("Still {ahead} to play ⚽",
         "Today stands at {tw} won, {tl} lost. Follow the rest on Tipsdad."),
    ],

    # ---------- VIP members: their matches are live ----------
    "vip_live": [
        ("👑 Your {pkg} match is live",
         "{match} just kicked off. Follow it in your VIP section now."),
        ("VIP kickoff 🔔",
         "{n} of your {pkg} matches are underway. Your seat is reserved."),
        ("{pkg}: game on ⚽",
         "{match} is live. Members get the full picture on Tipsdad."),
        ("Your VIP picks are in play 👑",
         "{n} {pkg} matches running now. Track them on Tipsdad."),
        ("Members' match is live 🔥",
         "{match} has started. Open your {pkg} section."),
    ],

    # ---------- VIP members: their tips won ----------
    "vip_won": [
        ("👑 {pkg} delivers ✅",
         "{match} came through for members. See it on Tipsdad."),
        ("VIP win ✅",
         "{wins} of your {pkg} tips just won. Nice one, member."),
        ("Your membership is working 💪",
         "{match} landed in {pkg}. Open Tipsdad for the next ones."),
        ("Members only: ✅",
         "{wins} {pkg} tip(s) just came good. Check your VIP section."),
        ("{pkg} green tick 👑",
         "{match}: done. More member predictions coming up on Tipsdad."),
    ],

    # ---------- VIP promotion / upgrade to Combined ----------
    "upsell": [
        ("{pkg} just won. Were you in? 👀",
         "VIP Combined unlocks every VIP package in one subscription. Open Tipsdad."),
        ("You're seeing part of the picture",
         "{pkg} landed today. VIP Combined gives you every package, every day."),
        ("One package is good. All is better. 👑",
         "Upgrade to VIP Combined and never miss a package that wins."),
        ("Why choose one? 🤔",
         "{pkg} came through today. VIP Combined unlocks them all. See plans on Tipsdad."),
        ("The full VIP menu 🍽️",
         "Under/Over, GG, Draws, Correct Score, Final Winner: all in VIP Combined."),
        ("{pkg} had a good day ✅",
         "Members with VIP Combined had it too. Upgrade in the VIP tab."),
        ("Level up your matchday 🚀",
         "VIP Combined: every premium prediction, one subscription. Open Tipsdad."),
        ("Don't watch from outside 👀",
         "{pkg} just delivered for members. VIP Combined puts you in every package."),
    ],

    # ---------- Comeback / renew VIP ----------
    "resubscribe": [
        ("Your VIP seat is still warm 👑",
         "{wins} VIP tips won today. Renew and get back in the action."),
        ("We kept your spot 🔑",
         "VIP had {wins} wins today. Come back and see what's next."),
        ("Missing VIP? It misses you too 😅",
         "{wins} VIP wins today. Renew your package on Tipsdad."),
        ("The VIP room is busy 🔥",
         "{wins} winning VIP tips today. Your comeback starts in the VIP tab."),
        ("Ready for round two? 🥊",
         "VIP posted {wins} wins today. Renew and catch tomorrow's predictions."),
        ("Back in the game? ⚽",
         "{wins} VIP tips came through today. One tap to renew."),
    ],
}

# ============================================================
# SPECIAL OCCASIONS (mixed in automatically)
# ============================================================

# Saturdays and Sundays: used for posted / countdown / live updates
WEEKEND: list[tuple[str, str]] = [
    ("Weekend football is HERE 🔥",
     "{n} matches on today's Tipsdad list. Clear your schedule."),
    ("Saturday mode: ON ⚽",
     "The biggest football day of the week. {n} matches analysed on Tipsdad."),
    ("Sofa. Snacks. Tipsdad. 🛋️",
     "{n} weekend matches ready for you. First kickoff at {kickoff}."),
    ("The weekend belongs to football 🏆",
     "{n} games, fresh analysis, non-stop action. Open Tipsdad."),
    ("Super Sunday energy 💥",
     "{n} matches and the analysis is ready. Don't miss a minute."),
    ("Weekend plans? Sorted. ✅",
     "{n} matches on Tipsdad today. First whistle {kickoff}."),
    ("Big weekend, big decisions 👀",
     "{n} matches broken down for you. See them on Tipsdad."),
    ("No work. Just football. ⚽",
     "{n} weekend matches are live on Tipsdad. Dive in."),
]

# Champions League, Europa, World Cup, AFCON, Euros, cup finals
BIG_COMPETITION: list[tuple[str, str]] = [
    ("{comp} night 🏆",
     "The biggest stage is back. See Tipsdad's analysis before kickoff."),
    ("🌟 {comp} is calling",
     "Europe's best go head to head tonight. Open Tipsdad and get ready."),
    ("Anthem time 🎶",
     "{comp} is on today's list. Tipsdad has the breakdown."),
    ("Big nights need big analysis 💥",
     "{comp} action today. See the predictions on Tipsdad."),
    ("This is why we watch ⚽🏆",
     "{comp} is here. Your matchday guide is ready on Tipsdad."),
    ("{comp}: no ordinary game",
     "Elite football tonight. Open Tipsdad before the first whistle."),
    ("Goosebumps loading… 🏆",
     "{comp} matches on today's list. Don't watch unprepared."),
    ("Only the best play tonight 👑",
     "{comp} is live on Tipsdad's list. Check the analysis now."),
]

# Light football banter (sent only when matches are on today)
BANTER: list[tuple[str, str]] = [
    ("Your mate thinks he knows football 😏",
     "Prove him wrong. Today's analysis is on Tipsdad."),
    ("Group chat experts are typing… ⌨️",
     "Check Tipsdad first. Then talk."),
    ("Your coach called 📞",
     "He said: read the analysis before kickoff. {n} matches on Tipsdad."),
    ("Even the referee checks Tipsdad 🟨",
     "Okay, maybe not. But you should: {n} matches today."),
    ("Football is simple 🤷‍♂️",
     "Until it isn't. Get the full picture on Tipsdad."),
    ("Who's winning the argument tonight? 🗣️",
     "Back it up with analysis. {n} matches on Tipsdad."),
]

# Free-user engagement (free tips, History, Telegram)
FREE_ENGAGEMENT: list[tuple[str, str]] = [
    ("Free tips are waiting ⚽",
     "Today's free predictions are up. No subscription needed: open Tipsdad."),
    ("Don't skip the free section 👀",
     "{n} matches on today's list, some free for everyone."),
    ("Check our record yourself 📊",
     "Every result, won or lost, is posted in Tipsdad History."),
    ("Join the conversation 💬",
     "Tips and results land on the Tipsdad Telegram channel too."),
    ("Your free matchday guide 📋",
     "Today's free tips are ready. First kickoff {kickoff}."),
    ("Football's better informed 🧠",
     "Open Tipsdad and see today's free analysis."),
]

BIG_WORDS = {
    "champions league": "Champions League",
    "europa league": "Europa League",
    "conference league": "Conference League",
    "world cup": "World Cup",
    "afcon": "AFCON",
    "africa cup": "AFCON",
    "euro": "the Euros",
    "copa america": "Copa América",
    "fa cup": "FA Cup",
}


# ============================================================
# RENDERING
# ============================================================

_used: list[str] = []      # recently sent, to avoid repeats


def _fields(t: tuple[str, str]) -> set[str]:
    return {f for s in t for _, f, _, _ in string.Formatter().parse(s) if f}


def _data(d: dict) -> dict:
    """Map the bot's event details to template placeholders."""
    names = d.get("matches_you_may_name") or []
    data = {
        "match": names[0] if names else None,
        "match2": names[1] if len(names) > 1 else None,
        "n": d.get("live_now") or d.get("matches") or d.get("total_matches"),
        "kickoff": d.get("kickoff") or d.get("first_kickoff"),
        "mins": d.get("minutes_to_kickoff"),
        "ahead": d.get("matches_still_ahead_today"),
        "wait": d.get("wait"),
        "wins": d.get("wins_now") or d.get("vip_wins_today"),
        "won": d.get("won"),
        "lost": d.get("lost"),
        "pkg": d.get("package"),
        "comp": big_competition(d.get("leagues") or []),
        "streak": d.get("win_streak") if (d.get("win_streak") or 0) >= 2 else None,
    }
    # Records only count when something was actually settled
    if (d.get("today_won") or 0) + (d.get("today_lost") or 0) > 0:
        data["tw"], data["tl"] = d.get("today_won", 0), d.get("today_lost", 0)
    if (d.get("yesterday_won") or 0) + (d.get("yesterday_lost") or 0) > 0:
        data["yw"], data["yl"] = d.get("yesterday_won", 0), d.get("yesterday_lost", 0)
    if not data.get("ahead"):
        data["ahead"] = d.get("matches_still_to_play") or None
    return {k: v for k, v in data.items() if v not in (None, "", [])}


def big_competition(leagues: list[str]) -> str | None:
    text = " ".join(leagues).lower()
    for key, name in BIG_WORDS.items():
        if key in text:
            return name
    return None


def candidates(event: str, d: dict, now: datetime | None = None,
               extra: bool = True) -> list[tuple[str, str]]:
    """Templates usable for this event and this data."""
    now = now or datetime.now()
    pool = list(LIBRARY.get(event, []))
    if extra and event in ("tips_posted", "kickoff_soon", "next_kickoff",
                           "live_update"):
        if now.weekday() >= 5:
            pool += WEEKEND
    if extra and event in ("tips_posted", "kickoff_soon", "next_kickoff",
                           "live", "live_update"):
        pool += BIG_COMPETITION
    data = _data(d)
    # Lines that name a weekday only on that day ("Super Sunday" on Sundays)
    days = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]
    today = days[now.weekday()]
    pool = [t for t in pool
            if not any(day in (t[0] + t[1]).lower() and day != today for day in days)]
    return [t for t in pool if _fields(t) <= data.keys()]


def render(event: str, d: dict, now: datetime | None = None,
           pool: list[tuple[str, str]] | None = None) -> dict | None:
    """A filled-in notification, never one sent recently."""
    data = _data(d)
    options = pool if pool is not None else candidates(event, d, now)
    options = [t for t in options if _fields(t) <= data.keys()]
    if not options:
        return None
    fresh = [t for t in options if t[0] not in _used] or options
    title, body = random.choice(fresh)
    _used.append(title)
    del _used[:-40]
    return {"title": title.format(**data)[:60],
            "body": body.format(**data)[:160]}


def extras(d: dict) -> dict | None:
    """Banter or free-engagement message (for the 30-second pulses)."""
    return render("banter", d, pool=BANTER + FREE_ENGAGEMENT)


def examples(event: str, k: int = 4) -> str:
    """A few library lines for Gemini to copy the tone (not the words)."""
    pool = LIBRARY.get(event, []) + (BANTER if event == "live_update" else [])
    picks = random.sample(pool, min(k, len(pool)))
    return "\n".join(f"- {t} | {b}" for t, b in picks)