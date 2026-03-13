"""
iPhone Found Email Watcher
==========================
Monitors your Gmail for Apple's "iPhone Found" alert email.
When detected, sends alerts to Discord and WhatsApp (multiple numbers).
Parses Apple's Spanish email format and includes a Google Maps link.
"""

import imaplib
import email
import time
import os
import re
import requests
from datetime import datetime, timedelta
from urllib.parse import quote
import sys

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────

CONFIG = {
    # --- Gmail ---
    "gmail_address":      os.environ["GMAIL_ADDRESS"],
    "gmail_app_password": os.environ["GMAIL_APP_PASSWORD"],

    # --- What email to watch for ---
    "watch_subject_keywords": ["iPhone", "encontró", "found", "located", "Find My"],
    "watch_from_keywords":    ["apple.com", "icloud.com"],

    # --- How often to check (seconds) ---
    "check_interval_seconds": 60,

    # ─── Discord ───
    "discord_enabled":     True,
    "discord_webhook_url": os.environ["DISCORD_WEBHOOK_URL"],

    # ─── WhatsApp via Twilio ───
    # Add as many numbers as you want via env vars WHATSAPP_TO, WHATSAPP_TO_2, WHATSAPP_TO_3
    "whatsapp_enabled":     True,
    "twilio_account_sid":   os.environ["TWILIO_ACCOUNT_SID"],
    "twilio_auth_token":    os.environ["TWILIO_AUTH_TOKEN"],
    "twilio_whatsapp_from": "whatsapp:+14155238886",
    "whatsapp_numbers": [
        os.environ.get("WHATSAPP_TO",   "whatsapp:+573174924147"),
        os.environ.get("WHATSAPP_TO_2", "whatsapp:+573156356850"),
        os.environ.get("WHATSAPP_TO_3", "whatsapp:+573153038988"),
    ],

    # ─── Windows Desktop Notification ───
    "windows_notification_enabled": False,
}

# ─────────────────────────────────────────────
#  PARSE APPLE'S EMAIL
# ─────────────────────────────────────────────

def parse_apple_iphone_email(subject, body):
    """
    Parses Apple's Spanish Find My email:
    'iPhone de Juan David se encontró cerca de Calle 20 28-49 Bucaramanga, Santander Colombia a la(s) 15:49 COT'
    Returns: (device_name, address, time_str, maps_url)
    """
    device_name = "Tu iPhone"
    address     = None
    time_str    = None
    maps_url    = None

    for text in [subject, body]:
        if not text:
            continue

        # Spanish format
        match = re.search(
            r'(iPhone[^\.]*?)se encontr[oó] cerca de (.+?) a la\(?s?\)?\s+([\d:]+)\s*(\w+)',
            text, re.IGNORECASE
        )
        if match:
            device_name = match.group(1).strip().rstrip("de ").strip()
            address     = match.group(2).strip()
            time_str    = f"{match.group(3)} {match.group(4)}"
            break

        # English fallback
        match_en = re.search(
            r'(iPhone[^\.]*?)was found near (.+?) at ([\d:]+ \w+)',
            text, re.IGNORECASE
        )
        if match_en:
            device_name = match_en.group(1).strip()
            address     = match_en.group(2).strip()
            time_str    = match_en.group(3).strip()
            break

    if address:
        maps_url = f"https://www.google.com/maps/search/?api=1&query={quote(address)}"

    return device_name, address, time_str, maps_url


# ─────────────────────────────────────────────
#  ALERT FUNCTIONS
# ─────────────────────────────────────────────

def send_discord_alert(subject, sender, body_preview, device_name, address, time_str, maps_url):
    if not CONFIG["discord_enabled"] or not CONFIG["discord_webhook_url"].startswith("http"):
        return
    try:
        fields = [
            {"name": "📧 Subject",      "value": subject, "inline": False},
            {"name": "📨 From",         "value": sender,  "inline": True},
            {"name": "🕐 Alert time",   "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": True},
        ]
        if address:
            fields.append({"name": "📍 Location",   "value": address,  "inline": False})
        if time_str:
            fields.append({"name": "⏰ Time found", "value": time_str, "inline": True})
        if maps_url:
            fields.append({"name": "🗺️ Google Maps","value": f"[📍 Open in Google Maps]({maps_url})", "inline": True})
        if body_preview:
            fields.append({"name": "📝 Preview",    "value": body_preview[:300] or "(empty)", "inline": False})

        message = {
            "embeds": [{
                "title":       f"🚨 {device_name} ENCONTRADO — REVISA YA!",
                "description": "Apple envió una alerta de Find My. ¡Tu iPhone puede haber sido localizado!",
                "color":       16711680,
                "fields":      fields,
                "footer":      {"text": "iPhone Watcher Script"}
            }]
        }
        r = requests.post(CONFIG["discord_webhook_url"], json=message, timeout=10)
        if r.status_code in (200, 204):
            print(f"[✓] Discord alert sent!")
        else:
            print(f"[✗] Discord failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[✗] Discord error: {e}")


def send_whatsapp_alerts(subject, device_name, address, time_str, maps_url):
    if not CONFIG["whatsapp_enabled"]:
        return
    try:
        from twilio.rest import Client
        client = Client(CONFIG["twilio_account_sid"], CONFIG["twilio_auth_token"])

        lines = [f"🚨 *{device_name} ENCONTRADO!*", ""]
        if address:
            lines.append(f"📍 *Lugar:* {address}")
        if time_str:
            lines.append(f"⏰ *Hora encontrado:* {time_str}")
        if maps_url:
            lines.append(f"🗺️ *Google Maps:* {maps_url}")
        lines += ["", f"📧 {subject}", f"🕐 Alerta recibida: {datetime.now().strftime('%H:%M:%S')}"]
        body = "\n".join(lines)

        numbers = [n for n in CONFIG["whatsapp_numbers"] if n and n.startswith("whatsapp:")]
        if not numbers:
            print("[!] No valid WhatsApp numbers found. Check WHATSAPP_TO env vars.")
            return

        for number in numbers:
            try:
                msg = client.messages.create(
                    body=body,
                    from_=CONFIG["twilio_whatsapp_from"],
                    to=number
                )
                print(f"[✓] WhatsApp sent to {number} — SID: {msg.sid}")
            except Exception as e:
                print(f"[✗] WhatsApp failed for {number}: {e}")

    except ImportError:
        print("[✗] twilio not installed. Run: pip install twilio")
    except Exception as e:
        print(f"[✗] WhatsApp error: {e}")


def fire_all_alerts(subject, sender, body):
    device_name, address, time_str, maps_url = parse_apple_iphone_email(subject, body)

    print(f"\n{'='*60}")
    print(f"🚨 MATCH FOUND!")
    print(f"   Device  : {device_name}")
    print(f"   Address : {address}")
    print(f"   Time    : {time_str}")
    print(f"   Maps    : {maps_url}")
    print(f"{'='*60}\n")

    send_discord_alert(subject, sender, body[:300], device_name, address, time_str, maps_url)
    send_whatsapp_alerts(subject, device_name, address, time_str, maps_url)


# ─────────────────────────────────────────────
#  EMAIL CHECKING
# ─────────────────────────────────────────────

def get_email_body(msg):
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
    subject_lower = subject.lower()
    sender_lower  = sender.lower()
    subject_match = any(kw.lower() in subject_lower for kw in CONFIG["watch_subject_keywords"])
    sender_match  = any(kw.lower() in sender_lower  for kw in CONFIG["watch_from_keywords"])
    return subject_match and sender_match


def check_gmail(already_seen_ids):
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(CONFIG["gmail_address"], CONFIG["gmail_app_password"])
        mail.select("inbox")

        date_str = (datetime.now() - timedelta(hours=24)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE {date_str})')

        if status != "OK":
            mail.logout()
            return already_seen_ids

        email_ids = messages[0].split()
        new_seen  = set(already_seen_ids)

        for eid in email_ids:
            eid_str = eid.decode()
            if eid_str in already_seen_ids:
                continue
            new_seen.add(eid_str)

            status, data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue

            raw_email = data[0][1]
            msg       = email.message_from_bytes(raw_email)
            subject   = str(msg.get("Subject", ""))
            sender    = str(msg.get("From", ""))
            body      = get_email_body(msg)

            if email_matches(subject, sender):
                fire_all_alerts(subject, sender, body)

        mail.logout()
        return new_seen

    except imaplib.IMAP4.error as e:
        print(f"[!] Gmail login error: {e}")
        return already_seen_ids
    except Exception as e:
        print(f"[!] Error checking Gmail: {e}")
        return already_seen_ids


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    numbers = [n for n in CONFIG["whatsapp_numbers"] if n and n.startswith("whatsapp:")]
    print("=" * 60)
    print("  📱 iPhone Found — Email Watcher")
    print("=" * 60)
    print(f"  Monitoring : {CONFIG['gmail_address']}")
    print(f"  Interval   : every {CONFIG['check_interval_seconds']}s")
    print(f"  Keywords   : {CONFIG['watch_subject_keywords']}")
    print()
    print("  Alerts:")
    if CONFIG["discord_enabled"]:  print("    ✓ Discord")
    if CONFIG["whatsapp_enabled"]: print(f"    ✓ WhatsApp → {len(numbers)} number(s)")
    print()
    print("  Press Ctrl+C to stop.")
    print("=" * 60)

    seen_ids = set()
    print("\n[*] Initial scan...")
    seen_ids = check_gmail(seen_ids)
    print("[*] Watching for NEW emails now.\n")

    while True:
        try:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] Checking...", end=" ", flush=True)
            seen_ids = check_gmail(seen_ids)
            print("OK")
            time.sleep(CONFIG["check_interval_seconds"])
        except KeyboardInterrupt:
            print("\n\n[*] Stopped.")
            sys.exit(0)
        except Exception as e:
            print(f"\n[!] Error: {e}")
            time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()