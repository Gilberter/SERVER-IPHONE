"""
iPhone Found Email Watcher — Persistent + Multi-number
=======================================================
- Redis persistence: no duplicate alerts on redeploy
- Multiple WhatsApp numbers via env vars
- Parses Apple Spanish/English Find My email
- Google Maps link from address
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
#  CONFIGURATION — all secrets via env vars
# ─────────────────────────────────────────────

def load_config():
    # Collect all WHATSAPP_TO, WHATSAPP_TO_2, WHATSAPP_TO_3... dynamically
    numbers = []
    # First number (required)
    n1 = os.environ.get("WHATSAPP_TO", "")
    if n1: numbers.append(n1 if n1.startswith("whatsapp:") else f"whatsapp:{n1}")
    # Extra numbers (optional, add as many as you want in Railway)
    i = 2
    while True:
        n = os.environ.get(f"WHATSAPP_TO_{i}", "")
        if not n:
            break
        numbers.append(n if n.startswith("whatsapp:") else f"whatsapp:{n}")
        i += 1

    return {
        "gmail_address":      os.environ["GMAIL_ADDRESS"],
        "gmail_app_password": os.environ["GMAIL_APP_PASSWORD"],

        "watch_subject_keywords": ["iPhone", "encontró", "encontro", "found", "located", "Find My"],
        "watch_from_keywords":    ["apple.com", "icloud.com", "apple"],

        "check_interval_seconds": int(os.environ.get("CHECK_INTERVAL", "60")),

        "discord_enabled":     True,
        "discord_webhook_url": os.environ.get("DISCORD_WEBHOOK_URL", ""),

        "whatsapp_enabled":     bool(numbers),

        # Primary Twilio account
        "twilio_account_sid":   os.environ.get("TWILIO_ACCOUNT_SID", ""),
        "twilio_auth_token":    os.environ.get("TWILIO_AUTH_TOKEN", ""),
        "twilio_whatsapp_from": "whatsapp:+14155238886",

        # Backup Twilio account (used automatically if primary hits 429 limit)
        "twilio_backup_sid":        os.environ.get("TWILIO_BACKUP_SID", ""),
        "twilio_backup_token":      os.environ.get("TWILIO_BACKUP_TOKEN", ""),
        "twilio_backup_from":       os.environ.get("TWILIO_BACKUP_FROM", "whatsapp:+14155238886"),

        "whatsapp_numbers":     numbers,
    }

# ─────────────────────────────────────────────
#  PERSISTENT STORAGE
# ─────────────────────────────────────────────

class RedisStorage:
    KEY = "iphone_watcher:seen_ids"

    def __init__(self, client):
        self.client = client

    def get_seen_ids(self):
        try:
            return set(self.client.smembers(self.KEY))
        except Exception as e:
            print(f"[!] Redis read error: {e}")
            return set()

    def save_seen_ids(self, new_ids):
        try:
            if new_ids:
                self.client.sadd(self.KEY, *new_ids)
            # Cap at 10000 to avoid unbounded growth
            size = self.client.scard(self.KEY)
            if size > 10000:
                self.client.spop(self.KEY, size - 10000)
        except Exception as e:
            print(f"[!] Redis write error: {e}")


class FileStorage:
    def __init__(self, path="seen_ids.txt"):
        self.path = path

    def get_seen_ids(self):
        try:
            if os.path.exists(self.path):
                with open(self.path) as f:
                    return set(line.strip() for line in f if line.strip())
        except Exception as e:
            print(f"[!] File read error: {e}")
        return set()

    def save_seen_ids(self, new_ids):
        try:
            existing = self.get_seen_ids()
            with open(self.path, "w") as f:
                f.write("\n".join(existing | new_ids))
        except Exception as e:
            print(f"[!] File write error: {e}")


def connect_storage():
    redis_url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_PRIVATE_URL")
    if redis_url:
        try:
            import redis
            client = redis.from_url(redis_url, decode_responses=True)
            client.ping()
            print("[✓] Redis connected — state persists across restarts.")
            return RedisStorage(client)
        except Exception as e:
            print(f"[!] Redis failed ({e}) — falling back to file storage.")
    else:
        print("[~] No REDIS_URL found — using file storage (resets on redeploy).")
    return FileStorage()

# ─────────────────────────────────────────────
#  PARSE APPLE EMAIL
# ─────────────────────────────────────────────

def parse_apple_email(subject, body):
    device_name = "Tu iPhone"
    address = time_str = maps_url = None

    for text in [subject, body]:
        if not text:
            continue

        # Spanish: "iPhone de X se encontró cerca de ADDRESS a la(s) HH:MM TZ"
        m = re.search(
            r'(iPhone[^\.]*?)se encontr[oó] cerca de (.+?) a la\(?s?\)?\s*([\d:]+)\s*(\w+)',
            text, re.IGNORECASE
        )
        if m:
            device_name = m.group(1).strip().rstrip("de").strip()
            address     = m.group(2).strip()
            time_str    = f"{m.group(3)} {m.group(4)}"
            break

        # English: "iPhone was found near ADDRESS at HH:MM TZ"
        m = re.search(
            r'(iPhone[^\.]*?)was found near (.+?) at ([\d:]+ \w+)',
            text, re.IGNORECASE
        )
        if m:
            device_name = m.group(1).strip()
            address     = m.group(2).strip()
            time_str    = m.group(3).strip()
            break

    if address:
        maps_url = f"https://www.google.com/maps/search/?api=1&query={quote(address)}"

    return device_name, address, time_str, maps_url

# ─────────────────────────────────────────────
#  ALERTS
# ─────────────────────────────────────────────

def send_discord(cfg, subject, sender, preview, device_name, address, time_str, maps_url):
    if not cfg["discord_enabled"] or not cfg["discord_webhook_url"].startswith("http"):
        return
    try:
        fields = [
            {"name": "📧 Subject",    "value": subject or "(none)", "inline": False},
            {"name": "📨 From",       "value": sender  or "(none)", "inline": True},
            {"name": "🕐 Alert time", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": True},
        ]
        if address:
            fields.append({"name": "📍 Location",    "value": address,  "inline": False})
        if time_str:
            fields.append({"name": "⏰ Time found",  "value": time_str, "inline": True})
        if maps_url:
            fields.append({"name": "🗺️ Google Maps", "value": f"[📍 Abrir en Google Maps]({maps_url})", "inline": True})
        if preview:
            fields.append({"name": "📝 Preview",     "value": preview[:300], "inline": False})

        payload = {"embeds": [{
            "title":       f"🚨 {device_name} ENCONTRADO — REVISA YA!",
            "description": "Apple envió una alerta de Find My. ¡Tu iPhone puede haber sido localizado!",
            "color":       16711680,
            "fields":      fields,
            "footer":      {"text": "iPhone Watcher"}
        }]}
        r = requests.post(cfg["discord_webhook_url"], json=payload, timeout=10)
        print(f"[✓] Discord sent!" if r.status_code in (200, 204) else f"[✗] Discord {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[✗] Discord error: {e}")


def send_single_whatsapp(client, from_number, to_number, body):
    """Send one WhatsApp message. Returns True on success, raises on failure."""
    msg = client.messages.create(body=body, from_=from_number, to=to_number)
    return msg.sid


def send_whatsapp(cfg, subject, device_name, address, time_str, maps_url):
    if not cfg["whatsapp_enabled"]:
        return
    if not cfg["twilio_account_sid"] or not cfg["twilio_auth_token"]:
        print("[!] Twilio credentials missing.")
        return
    try:
        from twilio.rest import Client

        # Build message body
        lines = [f"🚨 *{device_name} ENCONTRADO!*", ""]
        if address:  lines.append(f"📍 *Lugar:* {address}")
        if time_str: lines.append(f"⏰ *Hora:* {time_str}")
        if maps_url: lines.append(f"🗺️ *Mapa:* {maps_url}")
        lines += ["", f"📧 {subject}", f"🕐 {datetime.now().strftime('%H:%M:%S')}"]
        body = "\n".join(lines)

        # Primary client
        primary_client = Client(cfg["twilio_account_sid"], cfg["twilio_auth_token"])

        # Backup client (if configured)
        has_backup = bool(cfg["twilio_backup_sid"] and cfg["twilio_backup_token"])
        backup_client = Client(cfg["twilio_backup_sid"], cfg["twilio_backup_token"]) if has_backup else None

        for number in cfg["whatsapp_numbers"]:
            sent = False

            # Try primary first
            try:
                sid = send_single_whatsapp(primary_client, cfg["twilio_whatsapp_from"], number, body)
                print(f"[✓] WhatsApp (primary) → {number} ({sid})")
                sent = True
            except Exception as e:
                err = str(e)
                if "429" in err or "daily messages limit" in err.lower() or "exceeded" in err.lower():
                    print(f"[!] Primary Twilio hit rate limit for {number} — trying backup...")
                else:
                    print(f"[✗] Primary WhatsApp failed for {number}: {e}")

            # Try backup if primary failed
            if not sent:
                if backup_client:
                    try:
                        sid = send_single_whatsapp(backup_client, cfg["twilio_backup_from"], number, body)
                        print(f"[✓] WhatsApp (backup) → {number} ({sid})")
                        sent = True
                    except Exception as e2:
                        print(f"[✗] Backup WhatsApp also failed for {number}: {e2}")
                else:
                    print(f"[!] No backup Twilio configured. Set TWILIO_BACKUP_SID and TWILIO_BACKUP_TOKEN in Railway.")

    except ImportError:
        print("[✗] twilio not installed.")
    except Exception as e:
        print(f"[✗] WhatsApp error: {e}")


def fire_alerts(cfg, subject, sender, body):
    device_name, address, time_str, maps_url = parse_apple_email(subject, body)
    print(f"\n{'='*55}")
    print(f"🚨 MATCH! Device={device_name} | Addr={address} | Time={time_str}")
    print(f"{'='*55}\n")
    send_discord(cfg, subject, sender, body[:300], device_name, address, time_str, maps_url)
    send_whatsapp(cfg, subject, device_name, address, time_str, maps_url)

# ─────────────────────────────────────────────
#  EMAIL
# ─────────────────────────────────────────────

def get_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try: return part.get_payload(decode=True).decode("utf-8", errors="replace").strip()
                except: pass
    else:
        try: return msg.get_payload(decode=True).decode("utf-8", errors="replace").strip()
        except: pass
    return ""


def matches(cfg, subject, sender):
    s, f = subject.lower(), sender.lower()
    return (any(k.lower() in s for k in cfg["watch_subject_keywords"]) and
            any(k.lower() in f for k in cfg["watch_from_keywords"]))


def check_gmail(cfg, storage, seen_ids):
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(cfg["gmail_address"], cfg["gmail_app_password"])
        mail.select("inbox")

        date_str = (datetime.now() - timedelta(hours=24)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f"(SINCE {date_str})")
        if status != "OK":
            mail.logout()
            return seen_ids

        new_seen    = set(seen_ids)
        newly_added = set()

        for eid in data[0].split():
            eid_str = eid.decode()
            if eid_str in seen_ids:
                continue
            new_seen.add(eid_str)
            newly_added.add(eid_str)

            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue

            msg     = email.message_from_bytes(msg_data[0][1])
            subject = str(msg.get("Subject", ""))
            sender  = str(msg.get("From", ""))
            body    = get_body(msg)

            if matches(cfg, subject, sender):
                fire_alerts(cfg, subject, sender, body)

        mail.logout()

        if newly_added:
            storage.save_seen_ids(newly_added)

        return new_seen

    except imaplib.IMAP4.error as e:
        print(f"[!] Gmail auth error: {e}")
        return seen_ids
    except Exception as e:
        print(f"[!] Gmail error: {e}")
        return seen_ids

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    cfg     = load_config()
    storage = connect_storage()

    print("=" * 55)
    print("  📱 iPhone Watcher — Persistent")
    print("=" * 55)
    print(f"  Gmail    : {cfg['gmail_address']}")
    print(f"  Interval : {cfg['check_interval_seconds']}s")
    print(f"  Discord  : {'✓' if cfg['discord_enabled'] else '✗'}")
    print(f"  WhatsApp : {len(cfg['whatsapp_numbers'])} number(s) → {cfg['whatsapp_numbers']}")
    print("=" * 55)

    # Load previously seen IDs from Redis/file
    seen_ids = storage.get_seen_ids()
    if seen_ids:
        print(f"\n[✓] Loaded {len(seen_ids)} seen IDs — skipping old emails.")
    else:
        print("\n[*] First run — scanning existing emails to mark as seen...")
        seen_ids = check_gmail(cfg, storage, seen_ids)
        print("[*] Done. Only NEW emails will trigger alerts.\n")

    while True:
        try:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] Checking...", end=" ", flush=True)
            seen_ids = check_gmail(cfg, storage, seen_ids)
            print("OK")
            time.sleep(cfg["check_interval_seconds"])
        except KeyboardInterrupt:
            print("\n[*] Stopped.")
            sys.exit(0)
        except Exception as e:
            print(f"\n[!] Unexpected error: {e}")
            time.sleep(cfg["check_interval_seconds"])


if __name__ == "__main__":
    main()