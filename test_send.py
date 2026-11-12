"""
Send ONE test notification right now, to check the whole chain:
Firebase key → topic → app → pop-up → tap opens the right tab.

Usage:
    python test_send.py                         # "won" to everyone
    python test_send.py live                    # "live" to everyone
    python test_send.py vip_won vip_vipgg       # VIP GG subscribers only
    python test_send.py upsell upsell_combined  # single-package subscribers
    python test_send.py resubscribe vip_lapsed  # expired subscribers
    python test_send.py won free_users          # never-subscribed users

Tap result in the app:
    tips_posted, kickoff_soon, live          → Home
    won, results                             → History
    vip_live, vip_won, upsell, resubscribe   → VIP
"""

import json
import os
import sys

import firebase_admin
from firebase_admin import credentials, messaging

DEFAULT_TOPIC = "tipsdad"
ANDROID_CHANNEL_ID = "tipsdad_alerts"

MESSAGES = {
    "tips_posted": ("Today's tips are in ⚽ (test)", "Tapping opens Home."),
    "kickoff_soon": ("Kickoff in minutes ⏱ (test)", "Tapping opens Home."),
    "live": ("We're live 🔴 (test)", "Tapping opens Home."),
    "won": ("WON ✅ (test)", "Tapping opens History."),
    "results": ("Results are in 📊 (test)", "Tapping opens History."),
    "vip_live": ("Your VIP match is live 🔴 (test)", "Tapping opens VIP."),
    "vip_won": ("Your VIP tip WON ✅ (test)", "Tapping opens VIP."),
    "upsell": ("Get VIP Combined (test)", "Tapping opens VIP."),
    "resubscribe": ("Renew your VIP (test)", "Tapping opens VIP."),
}


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else "won"
    topic = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_TOPIC
    if event not in MESSAGES:
        sys.exit(f"Unknown event. Use one of: {', '.join(MESSAGES)}")

    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    if not raw and os.path.exists("service-account.json"):
        with open("service-account.json", encoding="utf-8") as f:
            raw = f.read()
    if not raw:
        sys.exit("No Firebase key: set FIREBASE_SERVICE_ACCOUNT or add "
                 "service-account.json to this folder.")

    firebase_admin.initialize_app(credentials.Certificate(json.loads(raw)))

    title, body = MESSAGES[event]
    message_id = messaging.send(messaging.Message(
        topic=topic,
        notification=messaging.Notification(title=title, body=body),
        data={"event": event},
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id=ANDROID_CHANNEL_ID,
                sound="default",
                default_vibrate_timings=True,
            ),
        ),
    ))
    print(f"Sent '{event}' to topic '{topic}': {message_id}")


if __name__ == "__main__":
    main()