"""
iPhone Found Email Watcher
==========================
Monitors your Gmail for Apple's "iPhone Found" alert email.
When detected, sends alerts to Discord, WhatsApp (Twilio), and/or Telegram.
Also shows a Windows desktop notification.

Setup instructions are in README.txt
"""

import imaplib
import email
import time
import os
import json
import requests
import smtplib
from datetime import datetime, timedelta
import sys

# ─────────────────────────────────────────────
#  CONFIGURATION — Edit these values!
# ─────────────────────────────────────────────

CONFIG = {
    # --- Gmail ---
    "gmail_address":      os.environ["GMAIL_ADDRESS"],
    "gmail_app_password": os.environ["GMAIL_APP_PASSWORD"],

    # --- What email to watch for ---
    "watch_subject_keywords": ["iPhone", "found", "located", "Find My"],
    "watch_from_keywords":    ["apple.com", "icloud.com"],

    # --- How often to check (seconds) ---
    "check_interval_seconds": 60,

    # ─── Discord ───
    "discord_enabled":     True,
    "discord_webhook_url": os.environ["DISCORD_WEBHOOK_URL"],

    # ─── WhatsApp via Twilio ───
    "whatsapp_enabled":      True,
    "twilio_account_sid":    os.environ["TWILIO_ACCOUNT_SID"],
    "twilio_auth_token":     os.environ["TWILIO_AUTH_TOKEN"],
    "twilio_whatsapp_from":  "whatsapp:+14155238886",
    "whatsapp_to":           os.environ["WHATSAPP_TO"],

    # ─── Windows Desktop Notification ───
    "windows_notification_enabled": False,
}

# ─────────────────────────────────────────────
#  ALERT FUNCTIONS
# ─────────────────────────────────────────────

def send_discord_alert(subject, sender, body_preview):
    """Send alert to Discord via webhook."""
    if not CONFIG["discord_enabled"] or not CONFIG["discord_webhook_url"].startswith("http"):
        return
    try:
        message = {
            "embeds": [{
                "title": "🚨 IPHONE ALERT — CHECK YOUR EMAIL NOW!",
                "description": f"A matching email was detected in your Gmail inbox.",
                "color": 16711680,  # red
                "fields": [
                    {"name": "📧 Subject", "value": subject, "inline": False},
                    {"name": "📨 From", "value": sender, "inline": True},
                    {"name": "🕐 Time", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": True},
                    {"name": "📝 Preview", "value": body_preview[:300] or "(empty)", "inline": False},
                ],
                "footer": {"text": "iPhone Watcher Script"}
            }]
        }
        r = requests.post(CONFIG["discord_webhook_url"], json=message, timeout=10)
        if r.status_code in (200, 204):
            print(f"[✓] Discord alert sent!")
        else:
            print(f"[✗] Discord failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[✗] Discord error: {e}")



def send_whatsapp_alert(subject, sender, body_preview):
    """Send WhatsApp alert via Twilio."""
    if not CONFIG["whatsapp_enabled"]:
        return
    try:
        from twilio.rest import Client
        client = Client(CONFIG["twilio_account_sid"], CONFIG["twilio_auth_token"])
        message = client.messages.create(
            body=(
                f"🚨 IPHONE ALERT!\n\n"
                f"Subject: {subject}\n"
                f"From: {sender}\n"
                f"Time: {datetime.now().strftime('%H:%M:%S')}\n\n"
                f"Preview: {body_preview[:200]}"
            ),
            from_=CONFIG["twilio_whatsapp_from"],
            to=CONFIG["whatsapp_to"]
        )
        print(f"[✓] WhatsApp alert sent! SID: {message.sid}")
    except ImportError:
        print("[✗] WhatsApp: twilio package not installed. Run: pip install twilio")
    except Exception as e:
        print(f"[✗] WhatsApp error: {e}")





def fire_all_alerts(subject, sender, body_preview):
    """Fire all configured alert channels."""
    print(f"\n{'='*60}")
    print(f"🚨 MATCH FOUND! Firing all alerts...")
    print(f"   Subject : {subject}")
    print(f"   From    : {sender}")
    print(f"   Preview : {body_preview[:100]}")
    print(f"{'='*60}\n")

    send_discord_alert(subject, sender, body_preview)
    send_whatsapp_alert(subject, sender, body_preview)


# ─────────────────────────────────────────────
#  EMAIL CHECKING
# ─────────────────────────────────────────────

def get_email_body(msg):
    """Extract plain text body from email."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    break
                except:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except:
            pass
    return body.strip()


def email_matches(subject, sender):
    """Check if email matches our watch criteria."""
    subject_lower = subject.lower()
    sender_lower = sender.lower()

    subject_match = any(kw.lower() in subject_lower for kw in CONFIG["watch_subject_keywords"])
    sender_match = any(kw.lower() in sender_lower for kw in CONFIG["watch_from_keywords"])

    return subject_match and sender_match


def check_gmail(already_seen_ids):
    """Connect to Gmail and check for matching emails. Returns new seen IDs."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(CONFIG["gmail_address"], CONFIG["gmail_app_password"])
        mail.select("inbox")

        # Search emails from last 24h to avoid false positives from old emails
        date_str = (datetime.now() - timedelta(hours=24)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE {date_str})')

        if status != "OK":
            mail.logout()
            return already_seen_ids

        email_ids = messages[0].split()
        new_seen = set(already_seen_ids)

        for eid in email_ids:
            eid_str = eid.decode()
            if eid_str in already_seen_ids:
                continue

            new_seen.add(eid_str)
            status, data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue

            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = str(msg.get("Subject", ""))
            sender = str(msg.get("From", ""))
            body = get_email_body(msg)

            if email_matches(subject, sender):
                fire_all_alerts(subject, sender, body)

        mail.logout()
        return new_seen

    except imaplib.IMAP4.error as e:
        print(f"[!] Gmail login error: {e}")
        print("    → Make sure you're using a Google App Password, not your real password.")
        print("    → Enable IMAP in Gmail Settings → See all settings → Forwarding and POP/IMAP")
        return already_seen_ids
    except Exception as e:
        print(f"[!] Error checking Gmail: {e}")
        return already_seen_ids


# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  📱 iPhone Found — Email Watcher")
    print("=" * 60)
    print(f"  Monitoring: {CONFIG['gmail_address']}")
    print(f"  Checking every {CONFIG['check_interval_seconds']} seconds")
    print(f"  Watching for subjects containing: {CONFIG['watch_subject_keywords']}")
    print(f"  From senders containing: {CONFIG['watch_from_keywords']}")
    print()
    print("  Alerts enabled:")
    if CONFIG["discord_enabled"]:   print("    ✓ Discord")
    if CONFIG["whatsapp_enabled"]:  print("    ✓ WhatsApp")
    print()
    print("  Press Ctrl+C to stop.")
    print("=" * 60)

    seen_ids = set()
    # On first run, mark all existing emails as already seen (don't re-alert old emails)
    print("\n[*] Initial scan — marking existing emails as seen...")
    seen_ids = check_gmail(seen_ids)
    print(f"[*] Done. Watching for NEW emails now...\n")

    while True:
        try:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] Checking Gmail...", end=" ", flush=True)
            seen_ids = check_gmail(seen_ids)
            print("OK")
            time.sleep(CONFIG["check_interval_seconds"])
        except KeyboardInterrupt:
            print("\n\n[*] Stopped by user. Goodbye!")
            sys.exit(0)
        except Exception as e:
            print(f"\n[!] Unexpected error: {e}")
            time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()