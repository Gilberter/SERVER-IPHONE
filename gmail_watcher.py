"""
iPhone Found Email Watcher
==========================
Persistent version — uses Redis to remember seen emails across restarts.
So you can redeploy/update without re-sending old alerts.
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
    "gmail_address":      os.environ["GMAIL_ADDRESS"],
    "gmail_app_password": os.environ["GMAIL_APP_PASSWORD"],

    "watch_subject_keywords": ["iPhone", "encontró", "found", "located", "Find My"],
    "watch_from_keywords":    ["apple.com", "icloud.com"],

    "check_interval_seconds": 60,

    "discord_enabled":     True,
    "discord_webhook_url": os.environ["DISCORD_WEBHOOK_URL"],

    "whatsapp_enabled":     True,
    "twilio_account_sid":   os.environ["TWILIO_ACCOUNT_SID"],
    "twilio_auth_token":    os.environ["TWILIO_AUTH_TOKEN"],
    "twilio_whatsapp_from": "whatsapp:+14155238886",
    "whatsapp_numbers": [
        os.environ.get("WHATSAPP_TO",   "whatsapp+573174924147"),
        os.environ.get("WHATSAPP_TO_2", ""),
        os.environ.get("WHATSAPP_TO_3", ""),
        os.environ.get("WHATSAPP_TO_4", ""),
        os.environ.get("WHATSAPP_TO_5", "whatsapp+573203446002"),
    ],

    "windows_notification_enabled": False,
}

# ─────────────────────────────────────────────
#  PERSISTENT STORAGE (Redis or local file)
# ─────────────────────────────────────────────

def get_storage():
    """
    Returns a storage object with get_seen_ids() and save_seen_ids().
    Uses Redis if REDIS_URL is set (Railway Redis), otherwise falls back
    to a local file (good for local testing).
    """
    redis_url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_PRIVATE_URL")

    if redis_url:
        try:
            import redis
            client = redis.from_url(redis_url, decode_responses=True)
            client.ping()
            print("[✓] Connected to Redis — seen IDs will persist across restarts.")
            return RedisStorage(client)
        except Exception as e:
            print(f"[!] Redis connection failed: {e}. Falling back to local file.")

    print("[~] Using local file storage (seen_ids.txt). Note: resets on Railway redeploy.")
    return FileStorage("seen_ids.txt")


class RedisStorage:
    KEY = "iphone_watcher:seen_ids"

    def __init__(self, client):
        self.client = client

    def get_seen_ids(self):
        try:
            members = self.client.smembers(self.KEY)
            return set(members)
        except Exception as e:
            print(f"[!] Redis read error: {e}")
            return set()

    def save_seen_ids(self, seen_ids):
        try:
            if seen_ids:
                self.client.sadd(self.KEY, *seen_ids)
            # Keep only last 10000 IDs to avoid unbounded growth
            size = self.client.scard(self.KEY)
            if size > 10000:
                # Remove random old members (Redis sets don't have order)
                excess = self.client.spop(self.KEY, size - 10000)
        except Exception as e:
            print(f"[!] Redis write error: {e}")


class FileStorage:
    def __init__(self, path):
        self.path = path

    def get_seen_ids(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r") as f:
                    return set(line.strip() for line in f if line.strip())
        except Exception as e:
            print(f"[!] File read error: {e}")
        return set()

    def save_seen_ids(self, seen_ids):
        try:
            with open(self.path, "w") as f:
                f.write("\n".join(seen_ids))
        except Exception as e:
            print(f"[!] File write error: {e}")


# ─────────────────────────────────────────────
#  PARSE APPLE EMAIL
# ─────────────────────────────────────────────

def parse_apple_iphone_email(subject, body):
    device_name = "Tu iPhone"
    address     = None
    time_str    = None
    maps_url    = None

    for text in [subject, body]:
        if not text:
            continue

        # Spanish: "iPhone de X se encontró cerca de ADDRESS a la(s) HH:MM TZ"
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
            {"name": "📧 Subject",    "value": subject, "inline": False},
            {"name": "📨 From",       "value": sender,  "inline": True},
            {"name": "🕐 Alert time", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": True},
        ]
        if address:
            fields.append({"name": "📍 Location",    "value": address,  "inline": False})
        if time_str:
            fields.append({"name": "⏰ Time found",  "value": time_str, "inline": True})
        if maps_url:
            fields.append({"name": "🗺️ Google Maps", "value": f"[📍 Abrir en Google Maps]({maps_url})", "inline": True})
        if body_preview:
            fields.append({"name": "📝 Preview",     "value": body_preview[:300] or "(empty)", "inline": False})

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
            lines.append(f"⏰ *Hora:* {time_str}")
        if maps_url:
            lines.append(f"🗺️ *Google Maps:* {maps_url}")
        lines += ["", f"📧 {subject}", f"🕐 {datetime.now().strftime('%H:%M:%S')}"]
        body = "\n".join(lines)

        numbers = [n for n in CONFIG["whatsapp_numbers"] if n and n.startswith("whatsapp:")]
        if not numbers:
            print("[!] No valid WhatsApp numbers configured.")
            return

        for number in numbers:
            try:
                msg = client.messages.create(
                    body=body,
                    from_=CONFIG["twilio_whatsapp_from"],
                    to=number
                )
                print(f"[✓] WhatsApp → {number} SID: {msg.sid}")
            except Exception as e:
                print(f"[✗] WhatsApp failed for {number}: {e}")

    except ImportError:
        print("[✗] twilio not installed.")
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


def check_gmail(already_seen_ids, storage):
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
        newly_added = set()

        for eid in email_ids:
            eid_str = eid.decode()
            if eid_str in already_seen_ids:
                continue

            new_seen.add(eid_str)
            newly_added.add(eid_str)

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

        # Persist any newly seen IDs
        if newly_added:
            storage.save_seen_ids(new_seen)

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
    print("  📱 iPhone Found — Email Watcher (Persistent)")
    print("=" * 60)
    print(f"  Monitoring : {CONFIG['gmail_address']}")
    print(f"  Interval   : every {CONFIG['check_interval_seconds']}s")
    print()
    print("  Alerts:")
    if CONFIG["discord_enabled"]:  print("    ✓ Discord")
    if CONFIG["whatsapp_enabled"]: print(f"    ✓ WhatsApp → {len(numbers)} number(s)")
    print()

    # Connect to storage
    storage  = get_storage()
    seen_ids = storage.get_seen_ids()

    if seen_ids:
        print(f"[✓] Loaded {len(seen_ids)} previously seen email IDs from storage.")
        print("[*] Skipping those — only NEW emails will trigger alerts.\n")
    else:
        print("[*] No previous state found. Doing initial scan to mark existing emails...\n")
        seen_ids = check_gmail(seen_ids, storage)
        print("[*] Done. Watching for NEW emails now.\n")

    while True:
        try:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] Checking...", end=" ", flush=True)
            seen_ids = check_gmail(seen_ids, storage)
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