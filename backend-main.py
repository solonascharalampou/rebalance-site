"""
Rebalance — full booking backend (deployable to Render).

Storage: SQLite on disk (DB_PATH). Persistent across requests.
External: Google Calendar (optional — works without it), PDF receipts.
Payments: Viva is FAKED for this test build — bookings are marked paid directly.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload
Open http://localhost:8000/docs
"""

import os
import json
import sqlite3
import hashlib
import secrets
import datetime as dt
import base64
import re
import io
import threading
import time
import shutil
import csv
from contextlib import contextmanager, asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from receipt import generate_receipt, next_receipt_number

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def _safe_path(env_name, default):
    """Use the configured path if we can actually write there, else fall back
    to the app folder. Lets the backend run on a free Render instance with no
    disk attached — data is temporary, but everything works for testing."""
    p = os.environ.get(env_name, default)
    d = os.path.dirname(p) or "."
    try:
        os.makedirs(d, exist_ok=True)
        test = os.path.join(d, ".write-test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        return p
    except Exception:
        fallback = os.path.basename(p)
        print(f"[storage] {p} not writable — using temporary {fallback} (data resets on restart)")
        return fallback


DB_PATH = _safe_path("DB_PATH", "rebalance.db")
RECEIPTS_DIR = _safe_path("RECEIPTS_DIR", "receipts")
ID_DOCS_DIR = _safe_path("ID_DOCS_DIR", "id-documents")
VAT_RATE = float(os.environ.get("VAT_RATE", "0.19"))
CANCEL_HOURS = float(os.environ.get("CANCEL_HOURS", "4"))
CAP = 5
CLASS_HOURS = ["08:00", "09:00", "10:00", "11:00", "17:00", "18:00", "19:00"]
CLASSES = {
    "reformer": {"name": "Reformer Pilates", "price": 35, "solo": 60},
    "rehab":    {"name": "Clinical Rehab",   "price": 42, "solo": 65, "ghs": True},
    "mat":      {"name": "Mat & Breath",     "price": 18, "solo": 40},
}
PACKAGES = {
    "p4":  {"name": "Starter · 4 classes",  "count": 4,  "price": 120},
    "p8":  {"name": "Monthly · 8 classes",  "count": 8,  "price": 220},
    "p3m": {"name": "3-Month · 30 classes", "count": 30, "price": 750},
}
TRAINER_EMAIL = os.environ.get("TRAINER_EMAIL", "trainer@rebalance.cy")
TRAINER_PASS = os.environ.get("TRAINER_PASS", "rebalance")  # CHANGE in Render


def _id_cipher():
    """Return a persistent local encryption key for identity documents.

    The key lives beside the SQLite database with owner-only permissions, so a
    copied upload directory alone cannot expose government-ID images. Back up
    the database directory and this key together.
    """
    from cryptography.fernet import Fernet
    key_path = os.environ.get("ID_DOCUMENT_KEY_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(DB_PATH)), ".rebalance-id.key")
    if os.environ.get("ID_DOCUMENT_KEY"):
        raw = os.environ["ID_DOCUMENT_KEY"].encode()
        key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    else:
        if not os.path.exists(key_path):
            os.makedirs(os.path.dirname(key_path), exist_ok=True)
            with open(key_path, "wb") as f:
                f.write(Fernet.generate_key())
            os.chmod(key_path, 0o600)
        with open(key_path, "rb") as f:
            key = f.read().strip()
    return Fernet(key)


def _save_id_document(user_id: int, side: str, data_url: str) -> tuple[str, str]:
    if not data_url or "," not in data_url:
        raise HTTPException(400, f"Upload the {side} of the ID")
    header, encoded = data_url.split(",", 1)
    mime = "image/jpeg" if "image/jpeg" in header else "image/png" if "image/png" in header else ""
    if not mime:
        raise HTTPException(400, "ID photos must be JPG or PNG images")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        raise HTTPException(400, f"The {side} ID image could not be read")
    if not 20_000 <= len(raw) <= 5 * 1024 * 1024:
        raise HTTPException(400, "Each ID photo must be between 20 KB and 5 MB")
    if mime == "image/jpeg" and not raw.startswith(b"\xff\xd8\xff"):
        raise HTTPException(400, "The uploaded JPG is not a valid image")
    if mime == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(400, "The uploaded PNG is not a valid image")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
        if width < 640 or height < 400:
            raise HTTPException(400, "ID photos must be at least 640 × 400 pixels so the details are readable")
        if width * height > 40_000_000:
            raise HTTPException(400, "The ID image dimensions are too large")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "The uploaded file is not a readable image")
    os.makedirs(ID_DOCS_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(ID_DOCS_DIR, f"user-{user_id}-{side}.bin")
    with open(path, "wb") as f:
        f.write(_id_cipher().encrypt(raw))
    os.chmod(path, 0o600)
    return path, mime

CALENDAR_ID = os.environ.get("CALENDAR_ID", "")
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_TIMEOUT = max(5, int(os.environ.get("SMTP_TIMEOUT", "12")))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "hello@rebalance.cy")
# Resend's SMTP username is literally "resend", which is not a valid From address —
# make sure a misconfiguration doesn't produce broken emails.
if "@" not in FROM_EMAIL:
    print(f"[email] FROM_EMAIL '{FROM_EMAIL}' is not an email address — "
          "set FROM_EMAIL to the address you send from (e.g. hello@rebalance.cy).")
    FROM_EMAIL = "onboarding@resend.dev"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # for the chatbot
CLINIKO_API_KEY = os.environ.get("CLINIKO_API_KEY", "").strip()
CLINIKO_SYNC_SECONDS = max(60, int(os.environ.get("CLINIKO_SYNC_SECONDS", "300")))
CLINIKO_USER_AGENT = os.environ.get(
    "CLINIKO_USER_AGENT", "Rebalance Cliniko Sync (hello@rebalance.cy)")


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
@contextmanager
def db():
    # timeout: wait for another writer instead of failing instantly with "database is locked"
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        # WAL lets readers and a writer work at the same time — far fewer locks
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE,
            pass_hash TEXT, credits INTEGER DEFAULT 0, token TEXT,
            totp_secret TEXT, totp_on INTEGER DEFAULT 0, phone TEXT);
        CREATE TABLE IF NOT EXISTS slots(
            id INTEGER PRIMARY KEY, date TEXT, hour TEXT, class_key TEXT,
            closed INTEGER DEFAULT 0, private INTEGER DEFAULT 0,
            spots TEXT, UNIQUE(date,hour));
        CREATE TABLE IF NOT EXISTS bookings(
            id INTEGER PRIMARY KEY, user_email TEXT, name TEXT,
            date TEXT, hour TEXT, class_key TEXT, spot INTEGER,
            private INTEGER, pay TEXT, amount REAL, status TEXT DEFAULT 'confirmed',
            receipt_no TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, n INTEGER);
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS passkeys(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT, credential_id TEXT UNIQUE, public_key TEXT,
            sign_count INTEGER DEFAULT 0, name TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS webauthn_challenges(
            email TEXT PRIMARY KEY, challenge TEXT, kind TEXT, created TEXT);
        -- shared studio state, so every device sees the same thing
        CREATE TABLE IF NOT EXISTS closures(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_from TEXT, date_until TEXT,
            days TEXT,          -- JSON list of weekday numbers, empty = all days
            times TEXT,         -- JSON list of times, empty = whole day
            reason TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS ghs_cover(
            email TEXT PRIMARY KEY, name TEXT, ref TEXT,
            status TEXT DEFAULT 'pending', requested TEXT, decided TEXT);
        CREATE TABLE IF NOT EXISTS regular_slots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT, name TEXT, class_key TEXT,
            weekday INTEGER, hour TEXT, solo INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending', requested TEXT);
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY, email TEXT, role TEXT, expires TEXT);
        CREATE TABLE IF NOT EXISTS codes(email TEXT PRIMARY KEY, code TEXT, expires TEXT, issued TEXT);
        CREATE TABLE IF NOT EXISTS waitlist(
            id INTEGER PRIMARY KEY, email TEXT, name TEXT,
            date TEXT, hour TEXT, class_key TEXT, created TEXT,
            notified INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reminders(
            booking_id INTEGER PRIMARY KEY, sent INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS campaign_codes(
            code TEXT PRIMARY KEY, campaign TEXT, discount REAL DEFAULT 0.10,
            used_by TEXT, used_at TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS email_delivery(
            id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT, subject TEXT,
            status TEXT, error TEXT, attempts INTEGER DEFAULT 1, created TEXT, sent_at TEXT);
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT,
            target TEXT, detail TEXT, created TEXT);
        """)
        # safe migration for databases created before 2FA existed
        cols = [r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "totp_secret" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
        if "totp_on" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN totp_on INTEGER DEFAULT 0")
        if "phone" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        for col, ddl in [
            ("id_number", "TEXT"), ("date_of_birth", "TEXT"), ("address", "TEXT"),
            ("id_front_path", "TEXT"), ("id_front_mime", "TEXT"),
            ("id_back_path", "TEXT"), ("id_back_mime", "TEXT"),
            ("id_status", "TEXT DEFAULT 'pending'"), ("id_submitted", "TEXT"),
            ("profile_photo", "TEXT"),
            ("cliniko_id", "TEXT"), ("cliniko_email", "TEXT"), ("cliniko_updated_at", "TEXT"),
            ("cliniko_synced_at", "TEXT"), ("cliniko_archived", "INTEGER DEFAULT 0"),
            ("cliniko_data", "TEXT"), ("account_active", "INTEGER DEFAULT 1"),
        ]:
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_id_number ON users(id_number) WHERE id_number IS NOT NULL AND id_number!=''")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_cliniko_id ON users(cliniko_id) WHERE cliniko_id IS NOT NULL AND cliniko_id!=''")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_cliniko_email ON users(cliniko_email)")
        bcols = [r["name"] for r in c.execute("PRAGMA table_info(bookings)").fetchall()]
        for col, ddl in [("viva_txn", "TEXT"), ("viva_order", "TEXT"),
                         ("refund_status", "TEXT"), ("refund_amount", "REAL"),
                         ("refund_ref", "TEXT"), ("refunded_at", "TEXT"),
                         ("attended", "INTEGER")]:
            if col not in bcols:
                c.execute(f"ALTER TABLE bookings ADD COLUMN {col} {ddl}")
        gcols = [r["name"] for r in c.execute("PRAGMA table_info(ghs_cover)").fetchall()]
        for col, ddl in [("expires", "TEXT"), ("session_limit", "INTEGER"),
                         ("sessions_used", "INTEGER DEFAULT 0")]:
            if col not in gcols:
                c.execute(f"ALTER TABLE ghs_cover ADD COLUMN {col} {ddl}")
        ccols = [r["name"] for r in c.execute("PRAGMA table_info(codes)").fetchall()]
        if "issued" not in ccols:
            c.execute("ALTER TABLE codes ADD COLUMN issued TEXT")


def get_or_make_slot(c, date, hour, class_key=None):
    row = c.execute("SELECT * FROM slots WHERE date=? AND hour=?", (date, hour)).fetchone()
    if row:
        return dict(row), json.loads(row["spots"])
    if class_key is None:
        idx = CLASS_HOURS.index(hour) if hour in CLASS_HOURS else 0
        class_key = list(CLASSES.keys())[idx % len(CLASSES)]
    spots = [None] * CAP
    c.execute("INSERT INTO slots(date,hour,class_key,closed,private,spots) VALUES(?,?,?,0,0,?)",
              (date, hour, class_key, json.dumps(spots)))
    row = c.execute("SELECT * FROM slots WHERE date=? AND hour=?", (date, hour)).fetchone()
    return dict(row), spots


def save_slot(c, slot, spots):
    c.execute("UPDATE slots SET closed=?,private=?,spots=?,class_key=? WHERE date=? AND hour=?",
              (slot["closed"], slot["private"], json.dumps(spots), slot["class_key"],
               slot["date"], slot["hour"]))


def next_counter(c, name="receipt"):
    row = c.execute("SELECT n FROM counters WHERE name=?", (name,)).fetchone()
    n = (row["n"] + 1) if row else 1
    c.execute("INSERT INTO counters(name,n) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET n=?",
              (name, n, n))
    return n


import bcrypt as _bcrypt
import time as _time
from collections import defaultdict as _dd

def hash_pw(pw, salt=None):
    """Legacy SHA-256 (kept only so old accounts can still log in and be migrated)."""
    return hashlib.sha256((salt + pw).encode()).hexdigest()


def hash_password(pw: str) -> str:
    """Store passwords with bcrypt — deliberately slow, so stolen hashes are hard to crack."""
    return "bcrypt$" + _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt(rounds=12)).decode()


def verify_password(pw: str, stored: str) -> bool:
    """Check a password against either a bcrypt hash or an old SHA-256 one."""
    if not stored:
        return False
    if stored.startswith("bcrypt$"):
        try:
            return _bcrypt.checkpw(pw.encode(), stored[7:].encode())
        except Exception:
            return False
    # legacy "salt:sha256" format
    if ":" in stored:
        salt, h = stored.split(":", 1)
        return hashlib.sha256((salt + pw).encode()).hexdigest() == h
    return False


def needs_rehash(stored: str) -> bool:
    return not (stored or "").startswith("bcrypt$")


# ---------------------------------------------------------------------------
# Rate limiting — stops password guessing and code brute-forcing
# ---------------------------------------------------------------------------
_hits = _dd(list)

def rate_limit(key: str, limit: int, window: int = 60):
    """Allow `limit` attempts per `window` seconds for this key, else 429."""
    now = _time.time()
    recent = [t for t in _hits[key] if now - t < window]
    _hits[key] = recent
    if len(recent) >= limit:
        wait = int(window - (now - recent[0])) + 1
        raise HTTPException(429, f"Too many attempts — please wait {wait} seconds and try again")
    _hits[key].append(now)


def rate_limit_check(key: str, limit: int, window: int = 60):
    """Check the limit WITHOUT counting this attempt — use before a password check,
    then only record it if the attempt actually failed. Successful logins never
    count against you."""
    now = _time.time()
    recent = [t for t in _hits[key] if now - t < window]
    _hits[key] = recent
    if len(recent) >= limit:
        wait = int(window - (now - recent[0])) + 1
        raise HTTPException(429, f"Too many failed attempts — please wait {wait} seconds and try again")


def rate_limit_fail(key: str):
    """Record a failed attempt."""
    _hits[key].append(_time.time())


def rate_limit_clear(key: str):
    """Wipe the record after a successful sign-in."""
    _hits.pop(key, None)


def client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


import smtplib, random
from email.message import EmailMessage

STUDIO_NAME = os.environ.get("STUDIO_NAME", "Rebalance Physiotherapy & Clinical Pilates")
STUDIO_SHORT = "Rebalance"
STUDIO_ADDR = os.environ.get("STUDIO_ADDRESS", "Angelou Terzaki 4, Engomi, Nicosia, Cyprus")
STUDIO_TEL = os.environ.get("STUDIO_TEL", "22 035001 · 96 489727")
STUDIO_IG = os.environ.get("STUDIO_INSTAGRAM", "@rebalance_physio_pilates")


def _email_html(heading, body_html, big_code=None, footer_note=None):
    """Wrap content in the studio's branded email template."""
    code_block = ""
    if big_code:
        code_block = f"""
        <tr><td align="center" style="padding:8px 0 22px">
          <div style="display:inline-block;background:#faf3f0;border:2px dashed #d4a8a0;border-radius:14px;
                      padding:18px 34px;font-family:Georgia,'Times New Roman',serif;font-size:34px;
                      letter-spacing:10px;color:#4a3b3e;font-weight:bold">{big_code}</div>
        </td></tr>"""
    note = ""
    if footer_note:
        note = f"""<p style="margin:18px 0 0;font-size:12px;color:#8a7a7c;line-height:1.6">{footer_note}</p>"""
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f6ede8;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6ede8;padding:28px 14px;">
<tr><td align="center">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#fffdfb;
         border-radius:18px;overflow:hidden;box-shadow:0 10px 30px -12px rgba(74,59,62,.25);">

    <!-- header -->
    <tr><td style="background:linear-gradient(135deg,#c77b7b,#8a9a6e);padding:26px 30px;" align="center">
      <div style="font-family:Georgia,'Times New Roman',serif;font-size:26px;letter-spacing:5px;
                  color:#ffffff;font-weight:normal">REBALANCE</div>
      <div style="font-size:11px;letter-spacing:2px;color:rgba(255,255,255,.85);margin-top:5px;
                  font-family:Helvetica,Arial,sans-serif;text-transform:uppercase">
        Physiotherapy &amp; Clinical Pilates</div>
    </td></tr>

    <!-- body -->
    <tr><td style="padding:32px 34px 8px;font-family:Helvetica,Arial,sans-serif;">
      <h1 style="margin:0 0 16px;font-family:Georgia,'Times New Roman',serif;font-weight:normal;
                 font-size:23px;color:#4a3b3e;">{heading}</h1>
      <div style="font-size:15px;color:#5a4c4e;line-height:1.65;">{body_html}</div>
    </td></tr>
    {code_block}

    <!-- footer -->
    <tr><td style="padding:22px 34px 30px;border-top:1px solid #efe2dd;
                   font-family:Helvetica,Arial,sans-serif;">
      {note}
      <p style="margin:14px 0 0;font-size:12px;color:#8a7a7c;line-height:1.7">
        <b style="color:#4a3b3e">{STUDIO_NAME}</b><br>
        {STUDIO_ADDR}<br>
        Tel {STUDIO_TEL}<br>
        {STUDIO_IG}
      </p>
    </td></tr>
  </table>
  <p style="max-width:520px;margin:16px auto 0;font-size:11px;color:#a8999b;
            font-family:Helvetica,Arial,sans-serif;line-height:1.6" align="center">
    You're receiving this because you have an account with {STUDIO_SHORT}.
  </p>
</td></tr></table>
</body></html>"""


def audit(action: str, actor: str = "system", target: str = "", detail=None):
    try:
        with db() as c:
            c.execute("INSERT INTO audit_log(actor,action,target,detail,created) VALUES(?,?,?,?,?)",
                      (actor, action, target,
                       json.dumps(detail or {}, ensure_ascii=False, default=str),
                       dt.datetime.now(dt.timezone.utc).isoformat()))
    except Exception as exc:
        print("Audit log failed:", exc)


def _record_email(recipient, subject, status, error=""):
    try:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with db() as c:
            c.execute("""INSERT INTO email_delivery(recipient,subject,status,error,created,sent_at)
                         VALUES(?,?,?,?,?,?)""",
                      (recipient, subject, status, str(error)[:1000], now,
                       now if status == "sent" else None))
    except Exception as exc:
        print("Email log failed:", exc)


def send_email(to_email, subject, body, html=None, reply_to=None):
    """Send one branded email with a timeout, one retry, and delivery logging."""
    recipient = (to_email or "").strip()
    if not recipient or "@" not in recipient:
        _record_email(recipient or "(empty)", subject, "failed", "Invalid recipient")
        return False
    if not SMTP_HOST:
        print(f"[email skipped — SMTP not set] to={recipient} subj={subject}")
        _record_email(recipient, subject, "skipped", "SMTP is not configured")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{STUDIO_NAME} <{FROM_EMAIL}>"
    msg["To"] = recipient
    msg["Reply-To"] = reply_to or FROM_EMAIL
    msg["X-Entity-Ref-ID"] = secrets.token_hex(12)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    last_error = None
    for attempt in range(2):
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(SMTP_USER, SMTP_PASS)
                refused = smtp.send_message(msg)
                if refused:
                    raise RuntimeError(f"Recipient refused: {refused}")
            _record_email(recipient, subject, "sent")
            return True
        except Exception as exc:
            last_error = exc
            print(f"Email attempt {attempt + 1} failed:", exc)
            if attempt == 0:
                time.sleep(1.0)
    _record_email(recipient, subject, "failed", last_error)
    return False

def make_code(c, email):
    """Issue a 6-digit code. If a valid one was sent in the last minute, reuse it —
    otherwise a double-tap on 'send code' would invalidate the code already emailed."""
    row = c.execute("SELECT code,expires,issued FROM codes WHERE email=?", (email,)).fetchone()
    now = dt.datetime.now()
    if row:
        try:
            still_valid = dt.datetime.fromisoformat(row["expires"]) > now
            issued = row["issued"] and dt.datetime.fromisoformat(row["issued"])
            just_sent = issued and (now - issued).total_seconds() < 60
            if still_valid and just_sent:
                return row["code"]          # reuse the one they already have
        except Exception:
            pass
    code = f"{random.randint(0, 999999):06d}"
    exp = (now + dt.timedelta(minutes=20)).isoformat()   # 20 min, not 10
    c.execute("INSERT INTO codes(email,code,expires,issued) VALUES(?,?,?,?) "
              "ON CONFLICT(email) DO UPDATE SET code=?,expires=?,issued=?",
              (email, code, exp, now.isoformat(), code, exp, now.isoformat()))
    return code

def check_code(c, email, code):
    row = c.execute("SELECT code,expires FROM codes WHERE email=?", (email,)).fetchone()
    if not row:
        return False
    if dt.datetime.fromisoformat(row["expires"]) < dt.datetime.now():
        return False
    ok = row["code"] == str(code).strip()
    if ok:
        c.execute("DELETE FROM codes WHERE email=?", (email,))
    return ok

def notify(email, subject, body, html=None, reply_to=None):
    """Send notifications off the request thread so booking buttons stay responsive."""
    threading.Thread(
        target=send_email,
        args=(email, subject, body),
        kwargs={"html": html, "reply_to": reply_to},
        daemon=True,
        name="rebalance-email",
    ).start()
    return True


# ----------------------------------------------------------------------------
# Google Calendar (optional — skipped if not configured)
# ----------------------------------------------------------------------------
def push_to_calendar(date, hour, class_key, spots, private):
    if not (CALENDAR_ID and GOOGLE_CREDENTIALS_FILE and os.path.exists(GOOGLE_CREDENTIALS_FILE)):
        return
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE, scopes=["https://www.googleapis.com/auth/calendar"])
        cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        start = dt.datetime.fromisoformat(f"{date}T{hour}:00")
        end = start + dt.timedelta(hours=1)
        booked = [s for s in spots if isinstance(s, dict)]
        title = f"{CLASSES[class_key]['name']} — {'PRIVATE' if private else f'{len(booked)}/{CAP}'}"
        body = {"summary": title,
                "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Nicosia"},
                "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Nicosia"},
                "extendedProperties": {"private": {"rebalance": "1", "key": f"{date}_{hour}"}}}
        existing = cal.events().list(calendarId=CALENDAR_ID,
                                     privateExtendedProperty=f"key={date}_{hour}",
                                     singleEvents=True).execute().get("items", [])
        if existing:
            cal.events().update(calendarId=CALENDAR_ID, eventId=existing[0]["id"], body=body).execute()
        else:
            cal.events().insert(calendarId=CALENDAR_ID, body=body).execute()
    except Exception as e:
        print("Calendar push failed (continuing):", e)


def issue_receipt(c, *, name, email, class_key, private, date, hour, amount, method):
    n = next_counter(c, "receipt")
    receipt_no = next_receipt_number(n)
    os.makedirs(RECEIPTS_DIR, exist_ok=True)
    try:
        generate_receipt(receipt_no=receipt_no, customer_name=name,
                          customer_email=email or "—",
                          session_name=CLASSES[class_key]["name"],
                          session_type="Private" if private else "Class of 5",
                          when_text=f"{date} · {hour}", amount=amount, method=method,
                          vat_rate=VAT_RATE, out_dir=RECEIPTS_DIR)
    except Exception as e:
        print("Receipt failed (continuing):", e)
    return receipt_no


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
app = FastAPI(title="Rebalance Booking API")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith(("/trainer", "/my-", "/ghs", "/backup")):
        response.headers["Cache-Control"] = "no-store, private"
    return response


def _cliniko_sync_worker():
    # Initial import shortly after boot, then keep Aphrodite's Cliniko edits
    # reflected in Rebalance at the configured interval.
    while True:
        try:
            sync_cliniko_patients()
        except Exception as exc:
            print("Cliniko sync failed (will retry):", exc)
        time.sleep(CLINIKO_SYNC_SECONDS)


@app.on_event("startup")
def start_cliniko_sync():
    if CLINIKO_API_KEY:
        threading.Thread(target=_cliniko_sync_worker, name="cliniko-sync", daemon=True).start()

# CORS — which website is allowed to talk to this API.
# Set ALLOWED_ORIGINS to your Render site, e.g.
#   ALLOWED_ORIGINS=https://rebalance.onrender.com,https://rebalance.cy
# Leave unset (or "*") only while testing.
_origins = os.environ.get("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = ["*"] if _origins.strip() == "*" else [o.strip() for o in _origins.split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])
init_db()  # ensure tables exist at import time


# ----------------------------------------------------------------------------
# Cliniko patient sync
# Cliniko is the source of truth for patient demographics and GHS custom fields.
# Rebalance remains the source of truth for login credentials, credits, loyalty,
# payments and bookings made in this app.
# ----------------------------------------------------------------------------
_cliniko_sync_lock = threading.Lock()


def _cliniko_shard() -> str:
    if not CLINIKO_API_KEY:
        return ""
    match = re.search(r"-([a-z]{2}\d{1,2})$", CLINIKO_API_KEY, re.I)
    return match.group(1).lower() if match else "au1"


def _cliniko_request(path: str, method: str = "GET", payload=None):
    if not CLINIKO_API_KEY:
        raise RuntimeError("CLINIKO_API_KEY is not configured")
    import urllib.request
    import urllib.error
    auth = base64.b64encode((CLINIKO_API_KEY + ":").encode()).decode()
    base = f"https://api.{_cliniko_shard()}.cliniko.com/v1"
    url = path if path.startswith("https://") else base + path
    if not url.startswith(base):
        raise RuntimeError("Cliniko returned an unexpected API URL")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Basic " + auth,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": CLINIKO_USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        # Do not copy Cliniko's response body into logs; it may contain patient data.
        raise RuntimeError(f"Cliniko API returned HTTP {exc.code}") from exc


def _cliniko_phone(patient: dict) -> str:
    numbers = patient.get("patient_phone_numbers") or []
    if not numbers:
        numbers = patient.get("phone_numbers") or []
    for phone in numbers:
        value = phone.get("number") or phone.get("normalized_number")
        if value:
            return str(value).strip()
    return ""


def _phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-8:] if len(digits) >= 8 else digits


def _cliniko_custom_values(patient: dict) -> dict:
    values = {}
    for section in (patient.get("custom_fields") or {}).get("sections", []):
        for field in section.get("fields", []):
            name = str(field.get("name") or "").strip().casefold()
            if name:
                values[name] = field.get("value")
    return values


def _cliniko_ghs(patient: dict):
    fields = _cliniko_custom_values(patient)
    ghs_fields = {name: value for name, value in fields.items()
                  if any(term in name for term in ("ghs", "gesy", "γεσυ", "γεσύ"))}
    concession = patient.get("concession_type") or {}
    concession_text = " ".join(str(concession.get(key) or "")
                                for key in ("name", "label", "description")).casefold()
    if any(term in concession_text for term in ("ghs", "gesy", "γεσυ", "γεσύ")):
        ghs_fields.setdefault("ghs concession", "approved")
    if not ghs_fields:
        return None, ""
    status_values = [str(value or "").strip().casefold() for name, value in ghs_fields.items()
                     if any(term in name for term in ("cover", "status", "eligible", "concession"))]
    if not status_values:
        status_values = [str(value or "").strip().casefold() for value in ghs_fields.values()]
    negative = {"", "no", "false", "0", "not covered", "rejected", "inactive", "όχι"}
    pending = {"pending", "requested", "awaiting", "in review", "εκκρεμεί"}
    status = ("rejected" if any(value in negative for value in status_values)
              else "pending" if any(value in pending for value in status_values)
              else "approved")
    ref = next((str(value) for name, value in ghs_fields.items()
                if value and any(term in name for term in ("ref", "number", "id", "code"))), "")
    return status, ref


def _cliniko_address(patient: dict) -> str:
    parts = [patient.get("address_1"), patient.get("address_2"), patient.get("address_3"),
             patient.get("city"), patient.get("state"), patient.get("post_code"),
             patient.get("country")]
    return ", ".join(str(part).strip() for part in parts if part and str(part).strip())


def _apply_cliniko_patient(c, patient: dict) -> str:
    cliniko_id = str(patient.get("id") or "").strip()
    if not cliniko_id:
        return "skipped"
    cliniko_email = str(patient.get("email") or "").strip().lower()
    phone = _cliniko_phone(patient)
    row = c.execute("SELECT * FROM users WHERE cliniko_id=?", (cliniko_id,)).fetchone()
    if not row and cliniko_email:
        row = c.execute("""SELECT * FROM users
                           WHERE lower(email)=? OR lower(cliniko_email)=?
                           ORDER BY account_active DESC LIMIT 1""",
                        (cliniko_email, cliniko_email)).fetchone()
    if not row and phone:
        key = _phone_key(phone)
        matches = [candidate for candidate in c.execute("SELECT * FROM users WHERE phone!=''").fetchall()
                   if _phone_key(candidate["phone"]) == key]
        row = matches[0] if len(matches) == 1 else None
    preferred = str(patient.get("preferred_first_name") or "").strip()
    first = preferred or str(patient.get("first_name") or "").strip()
    last = str(patient.get("last_name") or "").strip()
    name = " ".join(part for part in (first, last) if part).strip() or "Cliniko patient"
    address = _cliniko_address(patient)
    archived = 1 if patient.get("archived_at") else 0
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    raw = json.dumps(patient, ensure_ascii=False, separators=(",", ":"))
    if row:
        c.execute("""UPDATE users SET name=?,phone=?,date_of_birth=?,address=?,cliniko_id=?,
                     cliniko_email=?,cliniko_updated_at=?,cliniko_synced_at=?,cliniko_archived=?,
                     cliniko_data=? WHERE id=?""",
                  (name, phone, patient.get("date_of_birth") or "", address, cliniko_id,
                   cliniko_email, patient.get("updated_at") or "", now, archived, raw, row["id"]))
        result = "updated"
    else:
        internal_email = cliniko_email or f"cliniko-{cliniko_id}@patients.rebalance.local"
        if c.execute("SELECT 1 FROM users WHERE email=?", (internal_email,)).fetchone():
            internal_email = f"cliniko-{cliniko_id}@patients.rebalance.local"
        c.execute("""INSERT INTO users(name,email,phone,pass_hash,credits,token,date_of_birth,address,
                     id_status,cliniko_id,cliniko_email,cliniko_updated_at,cliniko_synced_at,
                     cliniko_archived,cliniko_data,account_active)
                     VALUES(?,?,?,?,0,NULL,?,?,'not_required',?,?,?,?,?,?,0)""",
                  (name, internal_email, phone, hash_password(secrets.token_urlsafe(32)),
                   patient.get("date_of_birth") or "", address, cliniko_id, cliniko_email,
                   patient.get("updated_at") or "", now, archived, raw))
        result = "created"
    status, ref = _cliniko_ghs(patient)
    if status:
        account_email = c.execute("SELECT email FROM users WHERE cliniko_id=?", (cliniko_id,)).fetchone()["email"]
        c.execute("""INSERT INTO ghs_cover(email,name,ref,status,requested,decided)
                     VALUES(?,?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET
                     name=excluded.name,ref=excluded.ref,status=excluded.status,decided=excluded.decided""",
                  (account_email, name, ref, status, now, now))
    return result


def _cliniko_patients():
    page = 1
    while True:
        result = _cliniko_request(f"/patients?page={page}&per_page=100&sort=updated_at:asc")
        patients = result.get("patients") or []
        for patient in patients:
            yield patient
        if len(patients) < 100:
            break
        page += 1


def sync_cliniko_patients() -> dict:
    if not CLINIKO_API_KEY:
        return {"configured": False, "created": 0, "updated": 0, "skipped": 0}
    counts = {"configured": True, "created": 0, "updated": 0, "skipped": 0, "pushed": 0}
    with _cliniko_sync_lock:
        patients = list(_cliniko_patients())
        with db() as c:
            for patient in patients:
                action = _apply_cliniko_patient(c, patient)
                counts[action] = counts.get(action, 0) + 1
            c.execute("""INSERT INTO settings(k,v) VALUES('cliniko_last_sync',?)
                         ON CONFLICT(k) DO UPDATE SET v=excluded.v""",
                      (dt.datetime.now(dt.timezone.utc).isoformat(),))
        # Any genuine app account still without a Cliniko ID did not match an
        # existing patient during the pull, so create it in Cliniko. A failed
        # push remains unlinked and is retried on the next cycle.
        with db() as c:
            missing = [dict(row) for row in c.execute("""SELECT * FROM users
                       WHERE account_active=1 AND (cliniko_id IS NULL OR cliniko_id='')
                       AND email!=? AND email NOT LIKE '%@patients.rebalance.local'""",
                       (TRAINER_EMAIL,)).fetchall()]
        for user in missing:
            try:
                patient = _cliniko_create_from_user(user)
                with db() as c:
                    _apply_cliniko_patient(c, patient)
                counts["pushed"] += 1
            except Exception as exc:
                print(f"Cliniko push failed for local user {user['id']} (will retry):", exc)
    return counts


def _cliniko_find_by_email(email: str):
    wanted = email.strip().casefold()
    for patient in _cliniko_patients():
        if str(patient.get("email") or "").strip().casefold() == wanted:
            return patient
    return None


def _cliniko_create_from_user(user: dict):
    pieces = user["name"].strip().split()
    return _cliniko_request("/patients", "POST", {
        "first_name": pieces[0] if pieces else "Rebalance",
        "last_name": " ".join(pieces[1:]) if len(pieces) > 1 else "Patient",
        "email": user["email"],
        "patient_phone_numbers": ([{"number": user["phone"], "phone_type": "Mobile"}]
                                  if user.get("phone") else []),
        "accepted_privacy_policy": True,
        "time_zone": "Asia/Nicosia",
    })


def ensure_cliniko_patient(email: str):
    """Link or create a Cliniko patient for a completed Rebalance signup."""
    if not CLINIKO_API_KEY:
        return None
    patient = _cliniko_find_by_email(email)
    if not patient:
        with db() as c:
            user = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            return None
        patient = _cliniko_create_from_user(dict(user))
    with db() as c:
        _apply_cliniko_patient(c, patient)
    return patient


def current_user(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(401, "Not signed in")
    with db() as c:
        sess = session_user(c, token)
        if not sess:
            raise HTTPException(401, "Session expired — please sign in again")
        row = c.execute("SELECT * FROM users WHERE email=?", (sess["email"],)).fetchone()
    if not row or not row["account_active"]:
        raise HTTPException(401, "Invalid session")
    return dict(row)


class SignUp(BaseModel):
    name: str
    email: str
    password: str
    phone: str = ""
    code: str = ""
    remember: bool = False
    date_of_birth: str = ""
    address: str = ""

class Login(BaseModel):
    email: str
    password: str
    code: str = ""          # emailed 6-digit verification code
    totp: str = ""          # 6-digit code from Google/Microsoft Authenticator
    remember: bool = False  # keep me signed in

class SendCode(BaseModel):
    email: str
    purpose: str = "login"

class ChatReq(BaseModel):
    messages: list
    context: str = ""

class BookReq(BaseModel):
    class_key: str
    date: str
    hour: str
    private: bool = False
    promo: Optional[str] = None

class WalkinReq(BaseModel):
    date: str
    hour: str
    spot: int
    name: str
    pay: str  # 'cash' | 'card'

class SlotAction(BaseModel):
    date: str
    hour: str
    spot: Optional[int] = None

class CampaignCodeReq(BaseModel):
    code: str

class CampaignGenerateReq(BaseModel):
    count: int = 50


@app.post("/campaign/validate")
def campaign_validate(req: CampaignCodeReq):
    code = req.code.strip().upper()
    with db() as c:
        row = c.execute(
            "SELECT code,discount FROM campaign_codes WHERE code=? AND used_by IS NULL",
            (code,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "That code is invalid or has already been used")
    return {"ok": True, "code": row["code"], "discount": row["discount"]}


@app.get("/health")
def health():
    # Warn loudly when data won't survive a restart — this has caused real confusion
    # (2FA set up, then wiped by the next deploy).
    temporary = not DB_PATH.startswith("/var/") and not os.path.isabs(DB_PATH)
    out = {"ok": True, "db": DB_PATH,
           "calendar": bool(CALENDAR_ID and GOOGLE_CREDENTIALS_FILE),
           "email": bool(SMTP_HOST),
           "cliniko": bool(CLINIKO_API_KEY),
           "storage": "TEMPORARY" if temporary else "persistent"}
    if temporary:
        out["warning"] = ("Data resets on every restart or redeploy — accounts, bookings "
                          "and 2FA setup will be lost. Attach a disk (or use your own "
                          "server) before real use.")
    with db() as c:
        out["accounts"] = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        out["bookings"] = c.execute("SELECT COUNT(*) n FROM bookings").fetchone()["n"]
    return out


@app.post("/send-code")
def send_code(req: SendCode, request: Request):
    # stop someone spamming codes at an address
    rate_limit("code:" + client_ip(request), limit=30, window=120)
    rate_limit("code:" + req.email, limit=20, window=180)
    with db() as c:
        code = make_code(c, req.email)
    send_email(req.email, "Your Rebalance security code",
               f"Your verification code is {code}. It expires in 20 minutes.\n\n"
               "If you didn't request this, ignore this email.\n\nRebalance, Engomi",
               html=_email_html(
                   "Your security code",
                   "<p style='margin:0'>Enter this code to confirm it's you. "
                   "It expires in <b>20 minutes</b>.</p>",
                   big_code=code,
                   footer_note="If you didn't request this code, you can safely ignore "
                               "this email — nothing has changed on your account."))
    # If email isn't set up yet, hand the code back so you can still test.
    # As soon as SMTP is configured this stops — the code only ever goes by email.
    if not SMTP_HOST:
        return {"ok": True, "sent": False, "test_code": code,
                "note": "Email not configured — code returned for testing only. "
                        "Set SMTP_HOST to send real emails."}
    return {"ok": True, "sent": True}


@app.post("/signup")
def signup(req: SignUp, request: Request):
    rate_limit("signup:" + client_ip(request), limit=10, window=180)
    with db() as c:
        normalized_email = req.email.strip().lower()
        existing = c.execute("""SELECT * FROM users WHERE lower(email)=? OR lower(cliniko_email)=?
                                ORDER BY account_active DESC LIMIT 1""",
                             (normalized_email, normalized_email)).fetchone()
        if existing and existing["account_active"]:
            raise HTTPException(409, "Email already registered")
        if not check_code(c, req.email, req.code):
            raise HTTPException(401, "That code isn't right, or it has expired. Tap “Resend code” for a new one.")
        legal_name = " ".join(req.name.strip().split())
        if len(legal_name) < 3:
            raise HTTPException(400, "Enter your full name")
        if existing:
            old_email = existing["email"]
            c.execute("""UPDATE users SET name=?,email=?,phone=?,pass_hash=?,account_active=1,
                         id_status='not_required' WHERE id=?""",
                      (legal_name, normalized_email, req.phone, hash_password(req.password), existing["id"]))
            if old_email != normalized_email:
                c.execute("UPDATE bookings SET user_email=? WHERE user_email=?", (normalized_email, old_email))
                c.execute("UPDATE regular_slots SET email=? WHERE email=?", (normalized_email, old_email))
                c.execute("UPDATE waitlist SET email=? WHERE email=?", (normalized_email, old_email))
                ghs = c.execute("SELECT * FROM ghs_cover WHERE email=?", (old_email,)).fetchone()
                if ghs:
                    c.execute("DELETE FROM ghs_cover WHERE email=?", (old_email,))
                    c.execute("""INSERT OR REPLACE INTO ghs_cover(email,name,ref,status,requested,decided)
                                 VALUES(?,?,?,?,?,?)""",
                              (normalized_email, ghs["name"], ghs["ref"], ghs["status"],
                               ghs["requested"], ghs["decided"]))
        else:
            c.execute("""INSERT INTO users(name,email,phone,pass_hash,credits,token,id_status,account_active)
                         VALUES(?,?,?,?,0,NULL,'not_required',1)""",
                      (legal_name, normalized_email, req.phone, hash_password(req.password)))
        token = make_session(c, normalized_email, "customer", req.remember)
        c.execute("UPDATE users SET token=? WHERE email=?", (token, normalized_email))
    cliniko_linked = False
    if CLINIKO_API_KEY:
        try:
            cliniko_linked = bool(ensure_cliniko_patient(normalized_email))
        except Exception as exc:
            # The account remains usable and the background sync retries later.
            print("Cliniko signup link failed (will retry):", exc)
    notify(req.email, "Welcome to Rebalance",
           f"Hi {req.name}, your account is ready. You can now book classes anytime. — Rebalance, Engomi",
           html=_email_html(
               f"Welcome, {req.name} 🌿",
               "<p style='margin:0 0 12px'>Your account is ready — you can book classes anytime.</p>"
               "<p style='margin:0'>We look forward to seeing you in the studio.</p>",
               footer_note="Need to change a booking? Cancel more than 4 hours before "
                           "and you can reschedule."))
    return {"ok": True, "token": token, "name": legal_name, "email": normalized_email,
            "credits": 0, "twofa": False, "id_status": "not_required",
            "cliniko_linked": cliniko_linked}


@app.post("/login")
def login(req: Login, request: Request):
    # Brute-force protection that doesn't punish honest people:
    # only FAILED attempts count, and a successful sign-in clears the record.
    ip_key = "login:" + client_ip(request)
    acct_key = "login:" + req.email
    rate_limit_check(ip_key, limit=20, window=120)
    rate_limit_check(acct_key, limit=8, window=120)
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (req.email,)).fetchone()
        if not row or not row["account_active"]:
            rate_limit_fail(ip_key); rate_limit_fail(acct_key)
            raise HTTPException(404, "NO_ACCOUNT")
        if not verify_password(req.password, row["pass_hash"]):
            rate_limit_fail(ip_key); rate_limit_fail(acct_key)
            raise HTTPException(401, "WRONG_PASSWORD")

        # If they use an authenticator, that IS the second factor — no emailed code
        # needed. Faster to sign in, and just as safe.
        if row["totp_on"]:
            if not req.totp:
                raise HTTPException(403, "2FA_REQUIRED")
            if not pyotp.TOTP(row["totp_secret"]).verify(req.totp, valid_window=1):
                rate_limit_fail(acct_key)
                raise HTTPException(401, "That authenticator code isn't right")
        else:
            # no authenticator — fall back to the emailed code
            if not check_code(c, req.email, req.code):
                raise HTTPException(401, "That code isn't right, or it has expired. Tap “Resend code” for a new one.")

        # quietly upgrade old SHA-256 passwords to bcrypt on successful login
        if needs_rehash(row["pass_hash"]):
            c.execute("UPDATE users SET pass_hash=? WHERE id=?",
                      (hash_password(req.password), row["id"]))

        token = make_session(c, req.email, "customer", req.remember)
        c.execute("UPDATE users SET token=? WHERE id=?", (token, row["id"]))
    rate_limit_clear(ip_key); rate_limit_clear(acct_key)   # signed in — forget past failures
    return {"ok": True, "token": token, "name": row["name"],
            "email": row["email"], "credits": row["credits"],
            "phone": row["phone"] or "", "profile_photo": row["profile_photo"] or "",
            "twofa": bool(row["totp_on"])}


@app.get("/availability")
def availability(date: str):
    out = []
    with db() as c:
        for hour in CLASS_HOURS:
            # a studio closure overrides everything else
            shut = is_closed(c, date, hour)
            row = c.execute("SELECT * FROM slots WHERE date=? AND hour=?", (date, hour)).fetchone()
            if not row:
                idx = CLASS_HOURS.index(hour)
                ck = list(CLASSES.keys())[idx % len(CLASSES)]
                out.append({"hour": hour, "class_key": ck, "class": CLASSES[ck]["name"],
                            "free": 0 if shut else CAP, "booked": 0,
                            "closed": shut, "private": False})
                continue
            spots = json.loads(row["spots"])
            booked = sum(1 for s in spots if isinstance(s, dict))
            held = sum(1 for s in spots if s == "held")
            closed = bool(row["closed"]) or shut
            out.append({"hour": hour, "class_key": row["class_key"],
                        "class": CLASSES[row["class_key"]]["name"],
                        "free": 0 if (row["private"] or closed) else CAP - booked - held,
                        "booked": booked, "closed": closed,
                        "private": bool(row["private"])})
    return {"date": date, "cap": CAP, "classes": out}


@app.get("/packages")
def packages():
    return PACKAGES

@app.post("/buy-package")
def buy_package(pkg_id: str, user=Depends(current_user)):
    if pkg_id not in PACKAGES:
        raise HTTPException(400, "Unknown package")
    add = PACKAGES[pkg_id]["count"]
    with db() as c:
        c.execute("UPDATE users SET credits=credits+? WHERE id=?", (add, user["id"]))
        row = c.execute("SELECT credits FROM users WHERE id=?", (user["id"],)).fetchone()
    return {"ok": True, "credits": row["credits"]}


@app.post("/book")
def book(req: BookReq, user=Depends(current_user)):
    if req.class_key not in CLASSES:
        raise HTTPException(400, "Unknown class")
    # can't book a session that's already been and gone
    try:
        when = dt.datetime.fromisoformat(f"{req.date}T{req.hour}")
    except Exception:
        raise HTTPException(400, "Invalid date or time")
    if when < dt.datetime.now() - dt.timedelta(minutes=5):
        raise HTTPException(400, "That time has already passed — please pick a later slot")
    with db() as c:
        if is_closed(c, req.date, req.hour):
            raise HTTPException(409, "The studio is closed at that time")
        slot, spots = get_or_make_slot(c, req.date, req.hour, req.class_key)
        if slot["closed"] or slot["private"]:
            raise HTTPException(409, "This class isn't available")
        # one place per person per slot
        if any(isinstance(s, dict) and s.get("email") == user["email"] for s in spots):
            raise HTTPException(409, "You're already booked into this session")
        urow = c.execute("SELECT credits FROM users WHERE id=?", (user["id"],)).fetchone()
        credits = urow["credits"]
        # Once the trainer approves GHS for an account, every physiotherapy and
        # Pilates booking is covered. This is enforced server-side for both
        # private and shared sessions, so the browser can never charge by mistake.
        ghs_covered = has_ghs(c, user["email"])
        use_credit = (not req.private) and credits > 0 and not ghs_covered
        if req.private:
            if any(isinstance(s, dict) or s == "held" for s in spots):
                raise HTTPException(409, "Time not free for a private session")
            idx = 0
            pay_method = "ghs" if ghs_covered else "viva"
            spots = [{"name": user["name"], "email": user["email"], "pay": pay_method,
                      "private": True, "ghs": bool(ghs_covered)}] + ["held"] * (CAP - 1)
            slot["private"] = 1
            # Keep the normal fee as income owed by GHS; the customer pays €0.
            amount = CLASSES[req.class_key]["solo"]
        else:
            idx = next((i for i, s in enumerate(spots) if s is None), -1)
            if idx == -1:
                raise HTTPException(409, "Class is full")
            pay = "ghs" if ghs_covered else ("credit" if use_credit else "viva")
            spots[idx] = {"name": user["name"], "email": user["email"], "pay": pay,
                          "ghs": bool(ghs_covered)}
            amount = CLASSES[req.class_key]["price"]
            if use_credit:
                amount = 0
                c.execute("UPDATE users SET credits=credits-1 WHERE id=?", (user["id"],))
        save_slot(c, slot, spots)
        is_ghs = bool(ghs_covered)
        if is_ghs:
            method = "GHS"           # customer pays nothing; claimed from ΓΕΣΥ
        elif use_credit:
            method = "Package"
        else:
            method = "Viva"
        # Écru partnership cards carry unique, one-use codes.
        # A valid unused code plus a genuinely new account is required.
        promo_applied = None
        campaign_row = None
        if req.promo and amount > 0 and not is_ghs and not use_credit:
            campaign_row = c.execute(
                "SELECT code,discount FROM campaign_codes "
                "WHERE code=? AND used_by IS NULL",
                (req.promo.strip().upper(),)
            ).fetchone()
            prior = c.execute(
                "SELECT 1 FROM bookings WHERE user_email=? AND status='confirmed' LIMIT 1",
                (user["email"],)
            ).fetchone()
            if campaign_row and not prior:
                amount = round(amount * (1.0 - float(campaign_row["discount"])), 2)
                promo_applied = campaign_row["code"]
                c.execute(
                    "UPDATE campaign_codes SET used_by=?,used_at=? "
                    "WHERE code=? AND used_by IS NULL",
                    (user["email"], dt.datetime.now().isoformat(), promo_applied)
                )
        receipt_no = issue_receipt(c, name=user["name"], email=user["email"],
                                   class_key=req.class_key, private=req.private,
                                   date=req.date, hour=req.hour, amount=amount, method=method)
        c.execute("""INSERT INTO bookings(user_email,name,date,hour,class_key,spot,private,pay,amount,status,receipt_no,created)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (user["email"], user["name"], req.date, req.hour, req.class_key, idx,
                   int(req.private), method.lower(), amount, "confirmed", receipt_no,
                   dt.datetime.now().isoformat()))
        if is_ghs:
            c.execute("UPDATE ghs_cover SET sessions_used=COALESCE(sessions_used,0)+1 WHERE email=?",
                      (user["email"],))
        push_to_calendar(req.date, req.hour, req.class_key, spots, req.private)
    # confirmation email to the customer (with receipt number)
    _cls_name = CLASSES[req.class_key]["name"]
    _kind = "Private session" if req.private else f"Class of {CAP}"
    notify(user["email"], "Your Rebalance booking is confirmed",
           f"Hi {user['name']},\n\nYour {_cls_name} ({_kind}) is booked for "
           f"{req.date} at {req.hour}.\nReceipt: {receipt_no}\n\n"
           "Need to change it? Cancel more than 4 hours before to reschedule.\n\n— Rebalance, Engomi",
           html=_email_html(
               "You're booked in ✓",
               f"<p style='margin:0 0 18px'>Hi {user['name']}, your session is confirmed.</p>"
               "<table cellpadding='0' cellspacing='0' style='width:100%;font-size:14px;color:#5a4c4e'>"
               f"<tr><td style='padding:9px 0;border-bottom:1px dashed #efe2dd;color:#8a7a7c'>Session</td>"
               f"<td align='right' style='padding:9px 0;border-bottom:1px dashed #efe2dd;font-weight:bold;color:#4a3b3e'>{_cls_name}</td></tr>"
               f"<tr><td style='padding:9px 0;border-bottom:1px dashed #efe2dd;color:#8a7a7c'>Type</td>"
               f"<td align='right' style='padding:9px 0;border-bottom:1px dashed #efe2dd;font-weight:bold;color:#4a3b3e'>{_kind}</td></tr>"
               f"<tr><td style='padding:9px 0;border-bottom:1px dashed #efe2dd;color:#8a7a7c'>When</td>"
               f"<td align='right' style='padding:9px 0;border-bottom:1px dashed #efe2dd;font-weight:bold;color:#4a3b3e'>{req.date} · {req.hour}</td></tr>"
               f"<tr><td style='padding:9px 0;color:#8a7a7c'>Receipt</td>"
               f"<td align='right' style='padding:9px 0;font-weight:bold;color:#4a3b3e'>{receipt_no}</td></tr>"
               "</table>",
               footer_note="Cancel more than 4 hours before and you can reschedule — "
                           "your payment moves to the new time."))
    notify(TRAINER_EMAIL, f"New booking · {_cls_name}",
               f"{user['name']} booked {_cls_name} on {req.date} at {req.hour}. "
               f"Payment: {method}. Receipt: {receipt_no}.")
    audit("booking.created", user["email"], str(receipt_no),
          {"date": req.date, "hour": req.hour, "class": req.class_key,
           "private": req.private, "method": method, "amount": amount})
    return {"ok": True, "spot": idx + 1, "private": req.private,
            "amount": amount, "receipt_no": receipt_no, "method": method,
            "customer_pays": 0 if method == "GHS" else amount,
            "ghs_covered": method == "GHS", "promo": promo_applied}


@app.get("/my-bookings")
def my_bookings(user=Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM bookings WHERE user_email=? ORDER BY created DESC",
                         (user["email"],)).fetchall()
    return [dict(r) for r in rows]


@app.post("/my-cancel")
def my_cancel(req: SlotAction, user=Depends(current_user)):
    # The studio's policy is 4 hours' notice. Inside that window the spot can't be
    # resold, so the customer is asked to phone instead of cancelling silently.
    try:
        when = dt.datetime.fromisoformat(f"{req.date}T{req.hour}")
        hours_left = (when - dt.datetime.now()).total_seconds() / 3600
    except Exception:
        hours_left = 99
    if 0 < hours_left < CANCEL_HOURS:
        raise HTTPException(409,
            f"Sessions can only be cancelled more than {CANCEL_HOURS} hours ahead. "
            "Please call the studio on 22 035001 and we'll do our best to help.")
    with db() as c:
        b = c.execute("SELECT * FROM bookings WHERE user_email=? AND date=? AND hour=? AND status='confirmed'",
                      (user["email"], req.date, req.hour)).fetchone()
        if not b:
            raise HTTPException(404, "Booking not found")
        slot, spots = get_or_make_slot(c, req.date, req.hour)
        if b["private"]:
            slot["private"] = 0
            spots = [None] * CAP
        elif 0 <= b["spot"] < len(spots):
            spots[b["spot"]] = None
        save_slot(c, slot, spots)
        c.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (b["id"],))
        push_to_calendar(req.date, req.hour, slot["class_key"], spots, bool(slot["private"]))
    notify(user["email"], "Your Rebalance booking was cancelled",
           f"Hi {user['name']},\n\nYour booking on {req.date} at {req.hour} has been cancelled. "
           "We hope to see you again soon.\n\n— Rebalance, Engomi",
           html=_email_html(
               "Booking cancelled",
               f"<p style='margin:0 0 10px'>Hi {user['name']}, your session on "
               f"<b>{req.date} at {req.hour}</b> has been cancelled.</p>"
               "<p style='margin:0'>We hope to see you again soon 🌿</p>",
               footer_note="Ready to rebook? Just sign in and pick a new time."))
    notify(TRAINER_EMAIL, "Booking cancelled",
               f"{user['name']} cancelled the booking on {req.date} at {req.hour}.")
    audit("booking.cancelled", user["email"], str(b["id"]),
          {"date": req.date, "hour": req.hour})
    notify_waitlist(req.date, req.hour)   # a spot just freed up — tell the next person
    return {"ok": True}


def require_trainer(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(401, "Trainer auth required")
    with db() as c:
        sess = session_user(c, token)
    if not sess or sess["role"] != "trainer":
        raise HTTPException(401, "Trainer auth required")
    return True

@app.post("/trainer/login")
def trainer_login(req: Login, request: Request):
    tkey = "tlogin:" + client_ip(request)
    rate_limit_check(tkey, limit=10, window=120)
    if not (secrets.compare_digest(req.email, TRAINER_EMAIL) and
            secrets.compare_digest(req.password, TRAINER_PASS)):
        rate_limit_fail(tkey)
        raise HTTPException(401, "Wrong trainer credentials")

    # if the trainer has set up an authenticator app, require the code
    with db() as c:
        row = c.execute("SELECT totp_secret, totp_on FROM users WHERE email=?",
                        (TRAINER_EMAIL,)).fetchone()
        if row and row["totp_on"]:
            if not req.totp:
                raise HTTPException(403, "2FA_REQUIRED")
            if not pyotp.TOTP(row["totp_secret"]).verify(req.totp, valid_window=1):
                raise HTTPException(401, "That authenticator code isn't right")
        token = make_session(c, TRAINER_EMAIL, "trainer", req.remember)
    rate_limit_clear(tkey)   # signed in — forget past failures
    return {"ok": True, "token": token, "twofa": bool(row and row["totp_on"])}

@app.post("/trainer/campaign-codes/generate")
def campaign_codes_generate(req: CampaignGenerateReq, _=Depends(require_trainer)):
    count = max(1, min(250, req.count))
    made = []
    with db() as c:
        while len(made) < count:
            code = "ECRU-" + secrets.token_hex(3).upper()
            try:
                c.execute(
                    "INSERT INTO campaign_codes(code,campaign,discount,created) VALUES(?,?,?,?)",
                    (code, "ecru", 0.10, dt.datetime.now().isoformat())
                )
                made.append(code)
            except sqlite3.IntegrityError:
                pass
    return {"ok": True, "codes": made}


@app.get("/trainer/campaign-codes")
def campaign_codes_list(_=Depends(require_trainer)):
    with db() as c:
        rows = c.execute(
            "SELECT code,discount,used_by,used_at,created FROM campaign_codes "
            "WHERE campaign='ecru' ORDER BY created DESC"
        ).fetchall()
    return {"codes": [dict(r) for r in rows]}


@app.get("/trainer/schedule")
def trainer_schedule(date: str, _=Depends(require_trainer)):
    out = []
    with db() as c:
        for hour in CLASS_HOURS:
            slot, spots = get_or_make_slot(c, date, hour)
            # show places held for approved regulars, and honour closures
            spots = apply_regular_holds(c, date, hour, spots)
            closed = bool(slot["closed"]) or is_closed(c, date, hour)
            out.append({"hour": hour, "class_key": slot["class_key"],
                        "class": CLASSES[slot["class_key"]]["name"],
                        "closed": closed, "private": bool(slot["private"]),
                        "spots": spots})
    return {"date": date, "cap": CAP, "classes": out}

@app.post("/trainer/walkin")
def trainer_walkin(req: WalkinReq, _=Depends(require_trainer)):
    with db() as c:
        slot, spots = get_or_make_slot(c, req.date, req.hour)
        if not (0 <= req.spot < CAP) or spots[req.spot] not in (None, "held"):
            raise HTTPException(409, "Spot not available")
        if req.pay not in ("cash", "card", "ghs"):
            raise HTTPException(400, "Payment type must be cash, card or GESY")
        is_ghs = req.pay == "ghs"
        spots[req.spot] = {"name": req.name, "pay": req.pay, "ghs": is_ghs}
        save_slot(c, slot, spots)
        amount = 0.0 if is_ghs else CLASSES[slot["class_key"]]["price"]
        issue_receipt(c, name=req.name, email="", class_key=slot["class_key"],
                      private=False, date=req.date, hour=req.hour,
                      amount=amount, method="GESY" if is_ghs else req.pay.capitalize())
        c.execute("""INSERT INTO bookings(user_email,name,date,hour,class_key,spot,private,pay,amount,status,created)
                     VALUES('',?,?,?,?,?,0,?,?, 'confirmed',?)""",
                  (req.name, req.date, req.hour, slot["class_key"], req.spot, req.pay,
                   amount, dt.datetime.now().isoformat()))
        push_to_calendar(req.date, req.hour, slot["class_key"], spots, bool(slot["private"]))
    return {"ok": True}

@app.post("/trainer/hold")
def trainer_hold(req: SlotAction, _=Depends(require_trainer)):
    with db() as c:
        slot, spots = get_or_make_slot(c, req.date, req.hour)
        if req.spot is not None:
            spots[req.spot] = "held"
        else:
            slot["closed"] = 1
            spots = ["held"] * CAP
        save_slot(c, slot, spots)
    return {"ok": True}

@app.post("/trainer/free")
def trainer_free(req: SlotAction, _=Depends(require_trainer)):
    with db() as c:
        slot, spots = get_or_make_slot(c, req.date, req.hour)
        if req.spot is not None:
            spots[req.spot] = None
        else:
            slot["closed"] = 0
            slot["private"] = 0
            spots = [None] * CAP
        save_slot(c, slot, spots)
    notify_waitlist(req.date, req.hour)   # space opened — tell the next person waiting
    return {"ok": True}

@app.get("/trainer/clients")
def trainer_clients(_=Depends(require_trainer)):
    """Return the complete client list and booking history in one read-only query."""
    with db() as c:
        users = {row["email"]: dict(row) for row in c.execute(
            "SELECT name,email,phone,profile_photo FROM users WHERE account_active=1"
        ).fetchall()}
        bookings = [dict(row) for row in c.execute(
            """SELECT id,user_email,name,date,hour,class_key,spot,private,pay,amount,
                      status,attended,created
               FROM bookings ORDER BY date DESC,hour DESC,id DESC"""
        ).fetchall()]
    grouped = {}
    for booking in bookings:
        email = (booking.get("user_email") or "").strip().lower()
        key = email or ("walkin:" + (booking.get("name") or "Walk-in").strip().lower())
        if key not in grouped:
            user = users.get(email, {})
            grouped[key] = {
                "name": user.get("name") or booking.get("name") or "Walk-in",
                "email": email,
                "phone": user.get("phone") or "",
                "profile_photo": user.get("profile_photo") or "",
                "bookings": [],
            }
        class_info = CLASSES.get(booking.get("class_key"), {})
        booking["class"] = class_info.get("name", booking.get("class_key") or "Session")
        grouped[key]["bookings"].append(booking)
    return {"clients": sorted(grouped.values(), key=lambda item: item["name"].casefold())}


@app.get("/trainer/earnings-detail")
def trainer_earnings_detail(date_from: str = "", date_to: str = "",
                            _=Depends(require_trainer)):
    """Detailed earnings rows for the portal, optionally limited to an ISO date range."""
    where = ["status='confirmed'"]
    args = []
    if date_from:
        where.append("date>=?")
        args.append(date_from[:10])
    if date_to:
        where.append("date<=?")
        args.append(date_to[:10])
    with db() as c:
        rows = [dict(row) for row in c.execute(
            """SELECT id,date,hour,class_key,private,name,user_email,pay,amount,
                      status,attended,refund_status,refund_amount
               FROM bookings WHERE """ + " AND ".join(where) +
            " ORDER BY date DESC,hour DESC,id DESC", args
        ).fetchall()]
    for row in rows:
        row["class"] = CLASSES.get(row.get("class_key"), {}).get(
            "name", row.get("class_key") or "Session")
    return {"rows": rows}


@app.get("/trainer/earnings")
def trainer_earnings(_=Depends(require_trainer)):
    with db() as c:
        rows = c.execute("""SELECT pay, amount, refund_status, refund_amount
                            FROM bookings WHERE status='confirmed'""").fetchall()
    totals = {"viva": 0.0, "card": 0.0, "cash": 0.0, "package": 0.0}
    refunded = 0.0
    for r in rows:
        amt = r["amount"] or 0
        # money paid back to the customer isn't income
        if r["refund_status"] == "refunded":
            back = r["refund_amount"] or amt
            refunded += back
            amt = max(0.0, amt - back)
        totals[r["pay"]] = totals.get(r["pay"], 0.0) + amt
    electronic = totals["viva"] + totals["card"]
    gross = electronic + totals["cash"]
    return {"viva": totals["viva"], "card": totals["card"], "cash": totals["cash"],
            "electronic": electronic, "cash_total": totals["cash"], "gross": gross,
            "refunded": round(refunded, 2), "sessions": len(rows)}


# ----------------------------------------------------------------------------
# AI chatbot — calls Claude with studio context. Key stays server-side.
# ----------------------------------------------------------------------------
@app.post("/chat")
def chat(req: ChatReq):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "Chatbot not configured")
    try:
        import urllib.request, json as _json
        payload = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 400,
            "system": req.context or "You are a helpful assistant for a Pilates studio.",
            "messages": [{"role": m["role"], "content": m["content"]} for m in req.messages][-12:],
        }
        r = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=_json.dumps(payload).encode(),
            headers={"content-type": "application/json",
                     "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(r, timeout=30) as resp:
            data = _json.loads(resp.read())
        reply = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return {"reply": reply or "Sorry, I didn't catch that — could you rephrase?"}
    except Exception as e:
        print("Chat failed:", e)
        raise HTTPException(500, "Chat failed")


# ----------------------------------------------------------------------------
# Earnings Excel export (professionally formatted, for the accountant)
# ----------------------------------------------------------------------------
@app.get("/trainer/earnings.xlsx")
def trainer_earnings_xlsx(_=Depends(require_trainer)):
    from fastapi.responses import StreamingResponse
    import io
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        raise HTTPException(503, "openpyxl not installed")

    with db() as c:
        rows = c.execute("""SELECT date,hour,class_key,private,name,pay,amount
                            FROM bookings WHERE status='confirmed' ORDER BY date,hour""").fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Earnings"
    ROSE = "8A5A6A"; INK = "4A3B3E"; LINE = "E0D4D0"
    thin = Border(bottom=Side(style="thin", color=LINE))

    ws.merge_cells("A1:G1"); ws["A1"] = "Rebalance Physiotherapy & Clinical Pilates"
    ws["A1"].font = Font(bold=True, size=15, color=INK)
    ws.merge_cells("A2:G2"); ws["A2"] = "Earnings report"
    ws["A2"].font = Font(italic=True, size=11, color="8A7A7C")
    ws["A3"] = "Generated"; ws["B3"] = dt.datetime.now().strftime("%d/%m/%Y %H:%M")

    hdr = ["Date", "Time", "Class", "Type", "Client", "Payment method", "Amount (€)"]
    ws.append([])
    ws.append(hdr)
    hrow = ws.max_row
    for cell in ws[hrow]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=ROSE)
        cell.alignment = Alignment(horizontal="left")

    totals = {"viva": 0.0, "card": 0.0, "cash": 0.0}
    for r in rows:
        cls = CLASSES.get(r["class_key"], {}).get("name", r["class_key"])
        method = (r["pay"] or "viva")
        if method == "credit": method = "viva"
        totals[method] = totals.get(method, 0.0) + (r["amount"] or 0)
        ws.append([r["date"], r["hour"], cls, "Private" if r["private"] else "Class",
                   r["name"], method.upper(), r["amount"] or 0])
        ws.cell(ws.max_row, 7).number_format = '€#,##0.00'
        for cell in ws[ws.max_row]:
            cell.border = thin

    electronic = totals["viva"] + totals["card"]
    gross = electronic + totals["cash"]
    ws.append([])
    for label, val, bold in [("Viva (electronic)", totals["viva"], False),
                             ("Card (electronic)", totals["card"], False),
                             ("Cash", totals["cash"], False),
                             ("Electronic total", electronic, True),
                             ("GROSS TOTAL", gross, True)]:
        ws.append(["", "", "", "", "", label, val])
        row = ws.max_row
        ws.cell(row, 7).number_format = '€#,##0.00'
        if bold:
            ws.cell(row, 6).font = Font(bold=True, size=12)
            ws.cell(row, 7).font = Font(bold=True, size=12, color=ROSE)

    widths = [12, 8, 22, 10, 20, 18, 14]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=rebalance-earnings.xlsx"})


# ============================================================================
# WAITLIST — join when a class is full; auto-notify when a spot frees up
# ============================================================================
class WaitReq(BaseModel):
    date: str
    hour: str
    class_key: str


@app.post("/waitlist/join")
def waitlist_join(req: WaitReq, user=Depends(current_user)):
    with db() as c:
        slot, spots = get_or_make_slot(c, req.date, req.hour, req.class_key)
        free = sum(1 for s in spots if s is None)
        if free > 0 and not slot["private"] and not slot["closed"]:
            raise HTTPException(400, "That class still has space — just book it")
        existing = c.execute(
            "SELECT 1 FROM waitlist WHERE email=? AND date=? AND hour=?",
            (user["email"], req.date, req.hour)).fetchone()
        if existing:
            raise HTTPException(409, "You're already on the waitlist for this class")
        c.execute("""INSERT INTO waitlist(email,name,date,hour,class_key,created,notified)
                     VALUES(?,?,?,?,?,?,0)""",
                  (user["email"], user["name"], req.date, req.hour, req.class_key,
                   dt.datetime.now().isoformat()))
        pos = c.execute("SELECT COUNT(*) n FROM waitlist WHERE date=? AND hour=? AND notified=0",
                        (req.date, req.hour)).fetchone()["n"]
    notify(user["email"], "You're on the waitlist",
           f"Hi {user['name']},\n\nYou're #{pos} on the waitlist for "
           f"{CLASSES[req.class_key]['name']} on {req.date} at {req.hour}.\n"
           "If a spot frees up we'll email you straight away.\n\n— Rebalance, Engomi")
    return {"ok": True, "position": pos}


@app.get("/waitlist/mine")
def waitlist_mine(user=Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM waitlist WHERE email=? ORDER BY created",
                         (user["email"],)).fetchall()
    return [dict(r) for r in rows]


@app.post("/waitlist/leave")
def waitlist_leave(req: WaitReq, user=Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM waitlist WHERE email=? AND date=? AND hour=?",
                  (user["email"], req.date, req.hour))
    return {"ok": True}


def notify_waitlist(date, hour):
    """Called whenever a spot frees up. Emails the next person in line."""
    with db() as c:
        row = c.execute("""SELECT * FROM waitlist WHERE date=? AND hour=? AND notified=0
                           ORDER BY created LIMIT 1""", (date, hour)).fetchone()
        if not row:
            return
        c.execute("UPDATE waitlist SET notified=1 WHERE id=?", (row["id"],))
        cls = CLASSES.get(row["class_key"], {}).get("name", "your class")
    notify(row["email"], "A spot just opened up!",
           f"Hi {row['name']},\n\nGood news — a spot has opened for {cls} "
           f"on {date} at {hour}.\n\nIt's first come, first served — book now "
           "before someone else takes it.\n\n— Rebalance, Engomi",
           html=_email_html(
               "A spot just opened up 🌿",
               f"<p style='margin:0 0 14px'>Hi {row['name']}, good news — a place has come "
               f"free in <b>{cls}</b> on <b>{date} at {hour}</b>.</p>"
               "<p style='margin:0'>It's first come, first served, so book soon "
               "before someone else takes it.</p>",
               footer_note="You're getting this because you joined the waitlist for this class."))


# ============================================================================
# DAY-BEFORE REMINDERS
# Call GET /tasks/send-reminders once a day (e.g. a free cron-job service).
# ============================================================================
@app.get("/tasks/send-reminders")
def send_reminders(key: str = ""):
    """Emails everyone booked for tomorrow. Protect with ?key=<TRAINER_PASS>."""
    if key != TRAINER_PASS:
        raise HTTPException(401, "Bad task key")
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    sent = 0
    with db() as c:
        rows = c.execute("""SELECT * FROM bookings
                            WHERE date=? AND status='confirmed' AND user_email<>''""",
                         (tomorrow,)).fetchall()
        for b in rows:
            done = c.execute("SELECT sent FROM reminders WHERE booking_id=?",
                             (b["id"],)).fetchone()
            if done and done["sent"]:
                continue
            cls = CLASSES.get(b["class_key"], {}).get("name", "your session")
            ok = send_email(
                b["user_email"], "Reminder: your Rebalance session tomorrow",
                f"Hi {b['name']},\n\nJust a reminder — you have {cls} tomorrow "
                f"({b['date']}) at {b['hour']}.\n\n"
                "Angelou Terzaki 4, Engomi, Nicosia.\n"
                "Need to change it? Cancel more than 4 hours before to reschedule.\n\n"
                "See you soon 🌿\n— Rebalance",
                html=_email_html(
                    "See you tomorrow 🌿",
                    f"<p style='margin:0 0 14px'>Hi {b['name']}, a quick reminder about "
                    "your session tomorrow.</p>"
                    "<table cellpadding='0' cellspacing='0' style='width:100%;font-size:14px'>"
                    f"<tr><td style='padding:9px 0;border-bottom:1px dashed #efe2dd;color:#8a7a7c'>Session</td>"
                    f"<td align='right' style='padding:9px 0;border-bottom:1px dashed #efe2dd;font-weight:bold;color:#4a3b3e'>{cls}</td></tr>"
                    f"<tr><td style='padding:9px 0;color:#8a7a7c'>When</td>"
                    f"<td align='right' style='padding:9px 0;font-weight:bold;color:#4a3b3e'>{b['date']} · {b['hour']}</td></tr>"
                    "</table>",
                    footer_note="Need to change it? Cancel more than 4 hours before to reschedule."))
            c.execute("INSERT INTO reminders(booking_id,sent) VALUES(?,?) "
                      "ON CONFLICT(booking_id) DO UPDATE SET sent=excluded.sent",
                      (b["id"], 1 if ok else 0))
            if ok:
                sent += 1
    return {"ok": True, "date": tomorrow, "found": len(rows), "emailed": sent}


# ============================================================================
# STUDIO SETTINGS — the Manage tab saves here, so edits are permanent + shared
# ============================================================================
class SettingsReq(BaseModel):
    data: dict


@app.get("/settings")
def get_settings():
    """Public: the website reads classes/prices/times from here."""
    with db() as c:
        row = c.execute("SELECT v FROM settings WHERE k='studio'").fetchone()
    return json.loads(row["v"]) if row else {}


@app.post("/settings")
def save_settings(req: SettingsReq, _=Depends(require_trainer)):
    """Trainer only: save everything from the Manage tab."""
    with db() as c:
        c.execute("INSERT INTO settings(k,v) VALUES('studio',?) "
                  "ON CONFLICT(k) DO UPDATE SET v=?",
                  (json.dumps(req.data), json.dumps(req.data)))
    return {"ok": True}


# ============================================================================
# BACKUP — download the whole database (trainer only)
# ============================================================================
BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups"))


def create_daily_backup():
    if not os.path.exists(DB_PATH):
        return None
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    target = os.path.join(BACKUP_DIR, f"rebalance-{dt.date.today().isoformat()}.db")
    if not os.path.exists(target):
        source = sqlite3.connect(DB_PATH, timeout=30)
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close(); source.close()
        os.chmod(target, 0o600)
    cutoff = dt.datetime.now().timestamp() - 30 * 86400
    for name in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, name)
        if name.endswith(".db") and os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)
    return target


def _backup_worker():
    while True:
        try:
            create_daily_backup()
        except Exception as exc:
            print("Daily backup failed:", exc)
        time.sleep(6 * 3600)


@app.on_event("startup")
def start_backup_worker():
    create_daily_backup()
    threading.Thread(target=_backup_worker, name="daily-backup", daemon=True).start()


@app.get("/backup")
def backup(_=Depends(require_trainer)):
    from fastapi.responses import FileResponse
    path = create_daily_backup()
    if not path:
        raise HTTPException(404, "No database yet")
    audit("backup.downloaded", TRAINER_EMAIL, os.path.basename(path))
    return FileResponse(path, media_type="application/octet-stream",
                        filename=os.path.basename(path))


@app.get("/trainer/backups")
def list_backups(_=Depends(require_trainer)):
    create_daily_backup()
    rows = []
    for name in sorted(os.listdir(BACKUP_DIR), reverse=True):
        path = os.path.join(BACKUP_DIR, name)
        if name.endswith(".db") and os.path.isfile(path):
            rows.append({"name": name, "bytes": os.path.getsize(path),
                         "created": dt.datetime.fromtimestamp(os.path.getmtime(path)).isoformat()})
    return {"backups": rows, "retention_days": 30}


@app.post("/trainer/backups/restore")
def restore_backup(name: str, _=Depends(require_trainer)):
    safe = os.path.basename(name)
    if safe != name or not safe.endswith(".db"):
        raise HTTPException(400, "Invalid backup name")
    source_path = os.path.join(BACKUP_DIR, safe)
    if not os.path.isfile(source_path):
        raise HTTPException(404, "Backup not found")
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    safety_name = "before-restore-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".db"
    safety_path = os.path.join(BACKUP_DIR, safety_name)
    current = sqlite3.connect(DB_PATH, timeout=30)
    safety = sqlite3.connect(safety_path)
    try:
        current.backup(safety)
    finally:
        safety.close(); current.close()
    source = sqlite3.connect(source_path, timeout=30)
    destination = sqlite3.connect(DB_PATH, timeout=30)
    try:
        source.backup(destination)
    finally:
        destination.close(); source.close()
    audit("backup.restored", TRAINER_EMAIL, safe, {"safety_backup": safety_name})
    return {"ok": True, "restored": safe, "safety_backup": safety_name}


@app.get("/trainer/email-delivery")
def email_delivery(limit: int = 100, _=Depends(require_trainer)):
    with db() as c:
        rows = c.execute("""SELECT id,recipient,subject,status,error,created,sent_at
                            FROM email_delivery ORDER BY id DESC LIMIT ?""",
                         (max(1, min(limit, 500)),)).fetchall()
    return {"emails": [dict(row) for row in rows]}


@app.get("/trainer/audit-log")
def trainer_audit_log(limit: int = 200, _=Depends(require_trainer)):
    with db() as c:
        rows = c.execute("""SELECT id,actor,action,target,detail,created
                            FROM audit_log ORDER BY id DESC LIMIT ?""",
                         (max(1, min(limit, 1000)),)).fetchall()
    return {"events": [dict(row) for row in rows]}


@app.get("/trainer/ghs-claims.csv")
def ghs_claims_csv(date_from: str = "", date_to: str = "",
                   _=Depends(require_trainer)):
    from fastapi.responses import StreamingResponse
    query = """SELECT date,hour,name,user_email,class_key,private,amount,receipt_no
               FROM bookings WHERE status='confirmed' AND lower(pay)='ghs'"""
    args = []
    if date_from:
        query += " AND date>=?"; args.append(date_from[:10])
    if date_to:
        query += " AND date<=?"; args.append(date_to[:10])
    query += " ORDER BY date,hour"
    with db() as c:
        rows = c.execute(query, args).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Date","Time","Patient","Email","Service","Type","Claim amount","Receipt"])
    for row in rows:
        writer.writerow([row["date"],row["hour"],row["name"],row["user_email"],
                         CLASSES.get(row["class_key"],{}).get("name",row["class_key"]),
                         "Private" if row["private"] else "Group",
                         row["amount"],row["receipt_no"]])
    data = io.BytesIO(out.getvalue().encode("utf-8-sig"))
    return StreamingResponse(data, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rebalance-ghs-claims.csv"})


# ============================================================================
# TWO-FACTOR AUTHENTICATION (TOTP — Google Authenticator / Microsoft Authenticator)
# ============================================================================
import pyotp, io, base64

def make_session(c, email, role, remember=False):
    """Create a login session. remember=True lasts 30 days, otherwise 12 hours."""
    token = secrets.token_urlsafe(32)
    days = 30 if remember else 0
    hours = 0 if remember else 12
    exp = dt.datetime.now() + dt.timedelta(days=days, hours=hours)
    c.execute("INSERT INTO sessions(token,email,role,expires) VALUES(?,?,?,?)",
              (token, email, role, exp.isoformat()))
    return token


def session_user(c, token):
    row = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    if dt.datetime.fromisoformat(row["expires"]) < dt.datetime.now():
        c.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    return dict(row)


class TotpSetupReq(BaseModel):
    email: str
    password: str


class TotpConfirmReq(BaseModel):
    email: str
    code: str


@app.post("/2fa/setup")
def totp_setup(req: TotpSetupReq):
    """Step 1: generate a secret + QR code for Google/Microsoft Authenticator."""
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (req.email,)).fetchone()
        if row:
            # normal customer account
            if not verify_password(req.password, row["pass_hash"]):
                raise HTTPException(401, "Wrong email or password")
        else:
            # the trainer lives in the environment, not the users table —
            # create a row for them the first time they set up 2FA
            if not (secrets.compare_digest(req.email, TRAINER_EMAIL) and
                    secrets.compare_digest(req.password, TRAINER_PASS)):
                raise HTTPException(401, "Wrong email or password")
            # OR IGNORE: if two requests race, only one row is created and neither errors
            c.execute("INSERT OR IGNORE INTO users(name,email,pass_hash,credits) VALUES(?,?,?,0)",
                      ("Studio", TRAINER_EMAIL, hash_password(TRAINER_PASS)))
        secret = pyotp.random_base32()
        c.execute("UPDATE users SET totp_secret=?, totp_on=0 WHERE email=?", (secret, req.email))

    uri = pyotp.TOTP(secret).provisioning_uri(name=req.email, issuer_name="Rebalance Studio")
    # render the QR as a data URL the browser can show directly
    try:
        import qrcode
        img = qrcode.make(uri)
        buf = io.BytesIO(); img.save(buf, format="PNG")
        qr = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        qr = ""
    return {"ok": True, "secret": secret, "qr": qr, "uri": uri}


@app.post("/2fa/confirm")
def totp_confirm(req: TotpConfirmReq):
    """Step 2: they type the 6-digit code from the app to prove it's set up."""
    with db() as c:
        row = c.execute("SELECT totp_secret FROM users WHERE email=?", (req.email,)).fetchone()
        if not row or not row["totp_secret"]:
            raise HTTPException(400, "Start the setup first")
        if not pyotp.TOTP(row["totp_secret"]).verify(req.code, valid_window=1):
            raise HTTPException(401, "That code isn't right — check the app and try again")
        c.execute("UPDATE users SET totp_on=1 WHERE email=?", (req.email,))
    return {"ok": True, "enabled": True}


@app.post("/2fa/disable")
def totp_disable(req: TotpSetupReq):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (req.email,)).fetchone()
        if not row or not verify_password(req.password, row["pass_hash"]):
            raise HTTPException(401, "Wrong email or password")
        c.execute("UPDATE users SET totp_on=0, totp_secret=NULL WHERE email=?", (req.email,))
    return {"ok": True, "enabled": False}


@app.get("/2fa/status")
def totp_status(email: str):
    with db() as c:
        row = c.execute("SELECT totp_on FROM users WHERE email=?", (email,)).fetchone()
    return {"enabled": bool(row and row["totp_on"])}


@app.post("/logout")
def logout(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "").strip()
    if token:
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
    return {"ok": True}


# ============================================================================
# PASSWORD RESET — request a code by email, then set a new password
# ============================================================================
class ResetRequest(BaseModel):
    email: str


class ResetConfirm(BaseModel):
    email: str
    code: str
    new_password: str


@app.post("/password/forgot")
def password_forgot(req: ResetRequest, request: Request):
    """Email a reset code. Always replies the same way so nobody can use this
    to discover which email addresses have accounts."""
    rate_limit("forgot:" + client_ip(request), limit=8, window=180)
    rate_limit("forgot:" + req.email, limit=5, window=300)
    with db() as c:
        row = c.execute("SELECT name FROM users WHERE email=?", (req.email,)).fetchone()
        if row:
            code = make_code(c, req.email)
            send_email(req.email, "Reset your Rebalance password",
                       f"Hi {row['name']},\n\nYour password reset code is {code}. "
                       "It expires in 10 minutes.\n\n"
                       "If you didn't ask to reset your password, you can ignore this "
                       "email — your account is unchanged.\n\n— Rebalance, Engomi",
                       html=_email_html(
                           "Reset your password",
                           f"<p style='margin:0'>Hi {row['name']}, enter this code to set a "
                           "new password. It expires in <b>10 minutes</b>.</p>",
                           big_code=code,
                           footer_note="If you didn't ask to reset your password, ignore this "
                                       "email — your account is unchanged."))
    # deliberately identical response whether or not the account exists
    out = {"ok": True, "sent": True}
    if not SMTP_HOST:
        # testing convenience only — disappears as soon as email is configured
        with db() as c:
            r = c.execute("SELECT code FROM codes WHERE email=?", (req.email,)).fetchone()
        if r:
            out["test_code"] = r["code"]
            out["note"] = "Email not configured — code shown for testing only."
    return out


@app.post("/password/reset")
def password_reset(req: ResetConfirm, request: Request):
    rate_limit("reset:" + client_ip(request), limit=15, window=180)
    if len(req.new_password) < 6:
        raise HTTPException(400, "Please choose a password of at least 6 characters")
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (req.email,)).fetchone()
        if not row or not row["account_active"]:
            raise HTTPException(401, "That code isn't right, or it has expired. Request a new one.")
        if not check_code(c, req.email, req.code):
            raise HTTPException(401, "That code isn't right, or it has expired. Request a new one.")
        c.execute("UPDATE users SET pass_hash=? WHERE email=?",
                  (hash_password(req.new_password), req.email))
        # security: sign out every existing session on this account
        c.execute("DELETE FROM sessions WHERE email=?", (req.email,))
    notify(req.email, "Your Rebalance password was changed",
           f"Hi {row['name']},\n\nYour password has just been changed and you've been "
           "signed out on all devices.\n\nIf this wasn't you, contact us immediately on "
           "22 035001.\n\n— Rebalance, Engomi",
           html=_email_html(
               "Your password was changed",
               f"<p style='margin:0 0 10px'>Hi {row['name']}, your password has just been "
               "changed and you've been signed out on all devices.</p>"
               "<p style='margin:0;color:#a8493b'><b>If this wasn't you, call us immediately "
               "on 22 035001.</b></p>"))
    return {"ok": True}


@app.get("/tasks/test-email")
def test_email(key: str = "", to: str = ""):
    """Send yourself one branded test email to check the mail setup.
    Usage: /tasks/test-email?key=<TRAINER_PASS>&to=you@example.com"""
    if key != TRAINER_PASS:
        raise HTTPException(401, "Bad key")
    if not to or "@" not in to:
        raise HTTPException(400, "Add ?to=your@email.com")
    if not SMTP_HOST:
        return {"ok": False, "reason": "SMTP_HOST is not set — add your mail settings first"}
    sent = send_email(
        to, "Rebalance — test email",
        "If you can read this, your email settings are working.",
        html=_email_html(
            "Your email is working 🌿",
            "<p style='margin:0 0 12px'>This is a test from your Rebalance booking system.</p>"
            "<p style='margin:0'>Verification codes, booking confirmations, receipts and "
            "reminders will all look like this.</p>",
            footer_note="You can delete this email — it was sent to check your setup."))
    return {"ok": sent, "to": to, "from": FROM_EMAIL,
            "note": "If ok is false, check the Render logs for the mail error."}


@app.get("/tasks/clear-limits")
def clear_limits(key: str = ""):
    """Clear all rate-limit counters — use if you've locked yourself out during testing.
    Usage: /tasks/clear-limits?key=<TRAINER_PASS>"""
    if key != TRAINER_PASS:
        raise HTTPException(401, "Bad key")
    n = len(_hits)
    _hits.clear()
    return {"ok": True, "cleared": n, "note": "All rate limits reset — try signing in again."}


@app.get("/me")
def me(user=Depends(current_user)):
    """Validate a saved session token and return the account — used by
    'remember me' to sign someone back in automatically on page load."""
    return {"ok": True, "name": user["name"], "email": user["email"],
            "phone": user.get("phone") or "", "credits": user["credits"],
            "profile_photo": user.get("profile_photo") or "",
            "twofa": bool(user.get("totp_on"))}


class ProfilePhotoUpdate(BaseModel):
    photo: str = ""


@app.put("/me/photo")
def update_profile_photo(req: ProfilePhotoUpdate, user=Depends(current_user)):
    photo = req.photo.strip()
    if photo:
        if not re.match(r"^data:image/(jpeg|png|webp);base64,", photo):
            raise HTTPException(400, "Use a JPG, PNG, or WebP photo")
        if len(photo) > 2_800_000:
            raise HTTPException(400, "Photo must be smaller than 2 MB")
    with db() as c:
        c.execute("UPDATE users SET profile_photo=? WHERE id=?", (photo or None, user["id"]))
    return {"ok": True, "profile_photo": photo}


class ProfileUpdate(BaseModel):
    name: str
    phone: str = ""


@app.patch("/me")
def update_me(req: ProfileUpdate, user=Depends(current_user)):
    name = " ".join(req.name.strip().split())
    phone = req.phone.strip()[:40]
    if len(name) < 2:
        raise HTTPException(400, "Enter your full name")
    with db() as c:
        c.execute("UPDATE users SET name=?, phone=? WHERE id=?", (name, phone, user["id"]))
    return {"ok": True, "name": name, "phone": phone}


class PreCheck(BaseModel):
    email: str
    password: str


@app.post("/precheck")
def precheck(req: PreCheck, request: Request):
    """Check email + password BEFORE sending a verification code, so wrong
    credentials are reported immediately instead of after the code step.
    Returns whether the account uses 2FA so the site knows which step is next."""
    rate_limit_check("precheck:" + client_ip(request), limit=20, window=120)
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (req.email,)).fetchone()
        if not row or not row["account_active"]:
            rate_limit_fail("precheck:" + client_ip(request))
            raise HTTPException(404, "NO_ACCOUNT")
        if not verify_password(req.password, row["pass_hash"]):
            rate_limit_fail("precheck:" + client_ip(request))
            raise HTTPException(401, "WRONG_PASSWORD")
        return {"ok": True, "twofa": bool(row["totp_on"])}


# ============================================================================
# GOOGLE SIGN-IN
# Verifies the ID token Google gives the browser, then creates or signs in the
# matching account. No password needed — Google has already proven who they are.
# ============================================================================
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")


class GoogleAuth(BaseModel):
    credential: str          # the ID token from Google Sign-In
    phone: str = ""          # only needed the first time they sign up
    remember: bool = True


def _verify_google_token(token: str) -> dict:
    """Ask Google whether this token is genuine, and who it belongs to."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in isn't set up on this server yet")
    try:
        import urllib.request, json as _json
        url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + token
        with urllib.request.urlopen(url, timeout=10) as r:
            info = _json.loads(r.read())
    except Exception:
        raise HTTPException(401, "Could not verify that Google sign-in")
    # the token must have been issued for *our* app
    if info.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(401, "That sign-in wasn't issued for this site")
    if info.get("email_verified") not in ("true", True):
        raise HTTPException(401, "That Google account has no verified email")
    if not info.get("email"):
        raise HTTPException(401, "Google didn't return an email address")
    return info


@app.post("/auth/google")
def auth_google(req: GoogleAuth, request: Request):
    rate_limit("google:" + client_ip(request), limit=20, window=120)
    info = _verify_google_token(req.credential)
    email = info["email"].lower().strip()
    name = info.get("name") or email.split("@")[0]
    provider_photo = (info.get("picture") or "").strip()

    with db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not row:
            # first time — create the account. Google has already verified the
            # email, so there's no code to send.
            c.execute("INSERT INTO users(name,email,phone,pass_hash,credits,profile_photo) VALUES(?,?,?,?,0,?)",
                      (name, email, req.phone, "", provider_photo or None))
            row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            new_account = True
        else:
            new_account = False
            if not row["account_active"]:
                c.execute("UPDATE users SET account_active=1,name=?,phone=? WHERE id=?",
                          (name, req.phone or row["phone"], row["id"]))
            # keep their phone if we didn't have one
            if req.phone and not row["phone"]:
                c.execute("UPDATE users SET phone=? WHERE id=?", (req.phone, row["id"]))
            # Import the provider avatar once, but never overwrite a picture
            # the customer selected in their Rebalance profile.
            if provider_photo and not row["profile_photo"]:
                c.execute("UPDATE users SET profile_photo=? WHERE id=?",
                          (provider_photo, row["id"]))

        # if they've turned on 2FA, Google alone isn't enough
        if row["totp_on"]:
            raise HTTPException(403, "2FA_REQUIRED")

        token = make_session(c, email, "customer", req.remember)
        c.execute("UPDATE users SET token=? WHERE id=?", (token, row["id"]))
        row = c.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()

    if new_account:
        notify(email, "Welcome to Rebalance",
               f"Hi {name}, your account is ready. You can now book classes anytime. "
               "— Rebalance, Engomi",
               html=_email_html(f"Welcome, {name} 🌿",
                   "<p style='margin:0 0 12px'>Your account is ready — you signed in with Google, "
                   "so there's no password to remember.</p>"
                   "<p style='margin:0'>We look forward to seeing you in the studio.</p>"))
    return {"ok": True, "token": token, "name": row["name"], "email": email,
            "credits": row["credits"], "twofa": False, "new_account": new_account,
            "profile_photo": row["profile_photo"] or "",
            "needs_phone": not (row["phone"] or req.phone)}


@app.get("/auth/google/config")
def google_config():
    """The site asks this to know whether to show the Google button."""
    return {"enabled": bool(GOOGLE_CLIENT_ID), "client_id": GOOGLE_CLIENT_ID}


# ============================================================================
# SIGN IN WITH SHOP
# Uses Shop's OpenID Connect authorization-code flow with PKCE. The client
# secret never reaches the browser; ID tokens are verified against Shop's JWKS.
# ============================================================================
SHOP_CLIENT_ID = os.environ.get("SHOP_CLIENT_ID", "")
SHOP_CLIENT_SECRET = os.environ.get("SHOP_CLIENT_SECRET", "")
SHOP_REDIRECT_URI = os.environ.get(
    "SHOP_REDIRECT_URI", "https://api.solonstudios.org/auth/shop/callback")
SHOP_FRONTEND_URL = os.environ.get(
    "SHOP_FRONTEND_URL", "https://rebalance.cy/app.html")
_shop_states = {}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _shop_enabled() -> bool:
    return bool(SHOP_CLIENT_ID and SHOP_CLIENT_SECRET and SHOP_REDIRECT_URI)


def _shop_redirect(params: dict):
    import urllib.parse
    # Session details live in the URL fragment, which browsers do not send to
    # web servers or access logs.
    return RedirectResponse(SHOP_FRONTEND_URL + "#" + urllib.parse.urlencode(params))


def _verify_shop_id_token(token: str, expected_nonce: str) -> dict:
    """Verify Shop's RS256 ID token and all security-sensitive claims."""
    import urllib.request, json as _json
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes
    try:
        head_s, payload_s, sig_s = token.split(".")
        header = _json.loads(_b64url_decode(head_s))
        payload = _json.loads(_b64url_decode(payload_s))
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise ValueError("Unsupported Shop token")
        with urllib.request.urlopen("https://accounts.shop.app/auth/jwks", timeout=10) as r:
            jwks = _json.loads(r.read())
        jwk = next(k for k in jwks.get("keys", []) if k.get("kid") == header["kid"])
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        key = rsa.RSAPublicNumbers(e, n).public_key()
        key.verify(_b64url_decode(sig_s), (head_s + "." + payload_s).encode(),
                   padding.PKCS1v15(), hashes.SHA256())
        now = int(time.time())
        if payload.get("iss") != "https://accounts.shop.app":
            raise ValueError("Wrong issuer")
        aud = payload.get("aud")
        if not (aud == SHOP_CLIENT_ID or isinstance(aud, list) and SHOP_CLIENT_ID in aud):
            raise ValueError("Wrong audience")
        if int(payload.get("exp", 0)) <= now:
            raise ValueError("Expired token")
        if payload.get("nonce") != expected_nonce:
            raise ValueError("Wrong nonce")
        if not payload.get("email"):
            raise ValueError("Missing email")
        return payload
    except Exception as exc:
        print("[shop-auth] token validation failed:", type(exc).__name__)
        raise HTTPException(401, "Could not verify that Shop sign-in")


@app.get("/auth/shop/config")
def shop_config():
    return {"enabled": _shop_enabled()}


@app.get("/auth/shop/start")
def shop_start():
    import urllib.parse
    if not _shop_enabled():
        raise HTTPException(503, "Shop sign-in isn't configured yet")
    # Remove abandoned attempts so this small in-memory store stays bounded.
    now = time.time()
    for old_state, item in list(_shop_states.items()):
        if item["expires"] < now:
            _shop_states.pop(old_state, None)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    nonce = secrets.token_urlsafe(32)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    _shop_states[state] = {
        "verifier": verifier, "nonce": nonce, "expires": now + 600}
    params = {
        "response_type": "code", "client_id": SHOP_CLIENT_ID,
        "redirect_uri": SHOP_REDIRECT_URI, "scope": "openid email",
        "state": state, "nonce": nonce, "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(
        "https://accounts.shop.app/oauth/authorize?" + urllib.parse.urlencode(params))


@app.get("/auth/shop/callback")
def shop_callback(code: str = "", state: str = "", error: str = ""):
    import urllib.parse, urllib.request, json as _json
    if error:
        return _shop_redirect({"shop_error": "cancelled"})
    attempt = _shop_states.pop(state, None)
    if not code or not attempt or attempt["expires"] < time.time():
        return _shop_redirect({"shop_error": "expired"})
    try:
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": SHOP_REDIRECT_URI,
            "code_verifier": attempt["verifier"],
            "client_id": SHOP_CLIENT_ID,
            "client_secret": SHOP_CLIENT_SECRET,
        }).encode()
        req = urllib.request.Request(
            "https://accounts.shop.app/oauth/token", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=12) as r:
            tokens = _json.loads(r.read())
        info = _verify_shop_id_token(tokens.get("id_token", ""), attempt["nonce"])
        email = info["email"].lower().strip()
        name = info.get("name") or email.split("@")[0]
        provider_photo = (info.get("picture") or "").strip()
        with db() as c:
            row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not row:
                c.execute("INSERT INTO users(name,email,phone,pass_hash,credits,profile_photo) VALUES(?,?,?,?,0,?)",
                          (name, email, "", "", provider_photo or None))
                row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                new_account = True
            else:
                new_account = False
                if not row["account_active"]:
                    c.execute("UPDATE users SET account_active=1 WHERE id=?", (row["id"],))
                if provider_photo and not row["profile_photo"]:
                    c.execute("UPDATE users SET profile_photo=? WHERE id=?",
                              (provider_photo, row["id"]))
            if row["totp_on"]:
                return _shop_redirect({"shop_error": "twofa"})
            session = make_session(c, email, "customer", True)
            c.execute("UPDATE users SET token=? WHERE id=?", (session, row["id"]))
            row = c.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        if new_account:
            notify(email, "Welcome to Rebalance",
                   f"Hi {name}, your account is ready. You can now book classes anytime. — Rebalance, Engomi",
                   html=_email_html(f"Welcome, {name} 🌿",
                       "<p style='margin:0 0 12px'>Your account is ready — you signed in with Shop, "
                       "so there is no password to remember.</p>"))
        return _shop_redirect({
            "shop_token": session, "email": email, "name": row["name"],
            "new": "1" if new_account else "0",
            "needs_phone": "0" if row["phone"] else "1",
        })
    except HTTPException:
        return _shop_redirect({"shop_error": "verification"})
    except Exception as exc:
        print("[shop-auth] callback failed:", type(exc).__name__)
        return _shop_redirect({"shop_error": "server"})


# ============================================================================
# SHARED STUDIO STATE — closures, GHS cover, regular slots
# These used to live in the browser, which meant the trainer's changes never
# reached customers on their own devices. Now everyone reads the same source.
# ============================================================================
import json as _json


class ClosureReq(BaseModel):
    date_from: str
    date_until: str
    days: list = []
    times: list = []
    reason: str = ""


def _closure_rows(c):
    return [dict(r) for r in c.execute("SELECT * FROM closures ORDER BY date_from").fetchall()]


def is_closed(c, date_str: str, hour: str) -> bool:
    """True if any closure rule covers this date and time."""
    try:
        dow = dt.date.fromisoformat(date_str).weekday()
        dow = (dow + 1) % 7          # python Mon=0 -> JS Sun=0
    except Exception:
        return False
    for r in _closure_rows(c):
        if not (r["date_from"] <= date_str <= r["date_until"]):
            continue
        days = _json.loads(r["days"] or "[]")
        if days and dow not in days:
            continue
        times = _json.loads(r["times"] or "[]")
        if times and hour not in times:
            continue
        return True
    return False


@app.get("/closures")
def list_closures():
    """Public — customers need this so closed times don't appear bookable."""
    with db() as c:
        return {"closures": _closure_rows(c)}


@app.post("/closures")
def add_closure(req: ClosureReq, _=Depends(require_trainer)):
    if req.date_until < req.date_from:
        raise HTTPException(400, "The end date is before the start date")
    with db() as c:
        c.execute("""INSERT INTO closures(date_from,date_until,days,times,reason,created)
                     VALUES(?,?,?,?,?,?)""",
                  (req.date_from, req.date_until, _json.dumps(req.days),
                   _json.dumps(req.times), req.reason, dt.datetime.now().isoformat()))
    return {"ok": True}


@app.delete("/closures/{cid}")
def remove_closure(cid: int, _=Depends(require_trainer)):
    with db() as c:
        c.execute("DELETE FROM closures WHERE id=?", (cid,))
    return {"ok": True}


# ---------- GHS cover ----------
class GhsReq(BaseModel):
    ref: str = ""
    expires: str = ""
    session_limit: Optional[int] = None


@app.post("/ghs/request")
def ghs_request(req: GhsReq, user=Depends(current_user)):
    with db() as c:
        c.execute("""INSERT INTO ghs_cover(email,name,ref,status,requested,expires,session_limit,sessions_used)
                     VALUES(?,?,?,'pending',?,?,?,0)
                     ON CONFLICT(email) DO UPDATE SET ref=?, status='pending', requested=?,
                     expires=?, session_limit=?""",
                  (user["email"], user["name"], req.ref, dt.datetime.now().isoformat(),
                   req.expires or None, req.session_limit,
                   req.ref, dt.datetime.now().isoformat(), req.expires or None,
                   req.session_limit))
    return {"ok": True, "status": "pending"}


@app.get("/ghs/mine")
def ghs_mine(user=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT * FROM ghs_cover WHERE email=?", (user["email"],)).fetchone()
        effective = has_ghs(c, user["email"]) if row else False
    if not row:
        return {"status": None, "ref": "", "covered": False}
    remaining = None
    if row["session_limit"] is not None:
        remaining = max(0, row["session_limit"] - (row["sessions_used"] or 0))
    status = row["status"]
    if status == "approved" and not effective:
        status = "expired"
    return {"status": status, "ref": row["ref"] or "", "covered": effective,
            "expires": row["expires"], "session_limit": row["session_limit"],
            "sessions_used": row["sessions_used"] or 0, "remaining": remaining}


@app.get("/ghs/all")
def ghs_all(_=Depends(require_trainer)):
    with db() as c:
        return {"requests": [dict(r) for r in
                c.execute("SELECT * FROM ghs_cover ORDER BY status, requested").fetchall()]}


@app.post("/ghs/decide")
def ghs_decide(email: str, status: str, expires: str = "",
               session_limit: Optional[int] = None, _=Depends(require_trainer)):
    if status not in ("approved", "declined"):
        raise HTTPException(400, "Status must be approved or declined")
    if session_limit is not None and session_limit < 1:
        raise HTTPException(400, "Session limit must be at least 1")
    with db() as c:
        c.execute("""UPDATE ghs_cover SET status=?, decided=?,
                     expires=CASE WHEN ?!='' THEN ? ELSE expires END,
                     session_limit=COALESCE(?,session_limit) WHERE email=?""",
                  (status, dt.datetime.now().isoformat(), expires, expires,
                   session_limit, email))
    audit("ghs.decision", TRAINER_EMAIL, email,
          {"status": status, "expires": expires, "session_limit": session_limit})
    return {"ok": True}


def has_ghs(c, email: str) -> bool:
    row = c.execute("""SELECT status,expires,session_limit,sessions_used
                       FROM ghs_cover WHERE email=?""", (email,)).fetchone()
    if not row or row["status"] != "approved":
        return False
    if row["expires"]:
        try:
            if dt.date.fromisoformat(row["expires"][:10]) < dt.date.today():
                return False
        except ValueError:
            return False
    return row["session_limit"] is None or (row["sessions_used"] or 0) < row["session_limit"]


# ---------- regular slots ----------
class RegularReq(BaseModel):
    class_key: str
    weekday: int
    hour: str
    solo: bool = False


@app.post("/regulars/request")
def regular_request(req: RegularReq, user=Depends(current_user)):
    with db() as c:
        existing = c.execute("""SELECT id FROM regular_slots
                                WHERE email=? AND weekday=? AND hour=?""",
                             (user["email"], req.weekday, req.hour)).fetchone()
        if existing:
            c.execute("UPDATE regular_slots SET status='pending', class_key=?, solo=? WHERE id=?",
                      (req.class_key, int(req.solo), existing["id"]))
        else:
            c.execute("""INSERT INTO regular_slots(email,name,class_key,weekday,hour,solo,status,requested)
                         VALUES(?,?,?,?,?,?,'pending',?)""",
                      (user["email"], user["name"], req.class_key, req.weekday,
                       req.hour, int(req.solo), dt.datetime.now().isoformat()))
    return {"ok": True, "status": "pending"}


@app.get("/regulars/mine")
def regulars_mine(user=Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM regular_slots WHERE email=?", (user["email"],)).fetchall()
    return {"slots": [dict(r) for r in rows]}


@app.get("/regulars/all")
def regulars_all(_=Depends(require_trainer)):
    with db() as c:
        return {"slots": [dict(r) for r in
                c.execute("SELECT * FROM regular_slots ORDER BY status, requested").fetchall()]}


@app.post("/regulars/decide")
def regulars_decide(slot_id: int, status: str, _=Depends(require_trainer)):
    if status not in ("approved", "declined"):
        raise HTTPException(400, "Status must be approved or declined")
    with db() as c:
        c.execute("UPDATE regular_slots SET status=? WHERE id=?", (status, slot_id))
    return {"ok": True}


@app.delete("/regulars/{slot_id}")
def regulars_remove(slot_id: int, _=Depends(require_trainer)):
    with db() as c:
        c.execute("DELETE FROM regular_slots WHERE id=?", (slot_id,))
    return {"ok": True}


class TrainerCancel(BaseModel):
    date: str
    hour: str
    spot: int
    notify_client: bool = True
    # what happens to the money / the customer:
    #   'reschedule' — invite them to pick another time (nothing refunded yet)
    #   'refund'     — give the money back
    #   'credit'     — add a class credit to their account instead
    #   'none'       — just cancel, sort it out separately
    outcome: str = "reschedule"
    reason: str = ""
    suggested_times: list = []


@app.post("/trainer/cancel-booking")
def trainer_cancel_booking(req: TrainerCancel, _=Depends(require_trainer)):
    """Let the studio cancel a client's booking — for phone cancellations,
    illness, or a class that has to be called off. The 4-hour rule doesn't
    apply here; the studio can always act."""
    with db() as c:
        slot, spots = get_or_make_slot(c, req.date, req.hour)
        if not (0 <= req.spot < len(spots)):
            raise HTTPException(400, "No such spot")
        occupant = spots[req.spot]
        if not isinstance(occupant, dict):
            raise HTTPException(404, "That spot isn't booked")
        email = occupant.get("email")
        name = occupant.get("name", "there")

        if slot["private"]:
            slot["private"] = 0
            spots = [None] * CAP
        else:
            spots[req.spot] = None
        save_slot(c, slot, spots)

        booking_id = None; booking_amount = 0; booking_pay = None
        if email:
            b = c.execute("""SELECT id, amount, pay FROM bookings
                             WHERE user_email=? AND date=? AND hour=? AND status='confirmed'""",
                          (email, req.date, req.hour)).fetchone()
            if b:
                booking_id = b["id"]; booking_amount = b["amount"]; booking_pay = b["pay"]
                c.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (b["id"],))
        push_to_calendar(req.date, req.hour, slot["class_key"], spots, bool(slot["private"]))

    # let the next person on the waitlist know a place has opened
    try:
        notify_waitlist(req.date, req.hour)
    except Exception:
        pass

    # What happens next depends on what the studio chose.
    refunded = 0.0
    credited = 0
    paid = float(booking_amount or 0)
    was_paid = paid > 0 and (booking_pay or "") not in ("ghs", "credit")

    if req.outcome == "credit" and email and paid > 0:
        with db() as c:
            c.execute("UPDATE users SET credits = credits + 1 WHERE email=?", (email,))
            if booking_id:
                c.execute("UPDATE bookings SET refund_ref='credited' WHERE id=?", (booking_id,))
        credited = 1
    elif req.outcome == "refund" and email and was_paid:
        with db() as c:
            if booking_id:
                c.execute("""UPDATE bookings SET refund_status='refunded', refund_amount=?,
                             refund_ref=?, refunded_at=? WHERE id=?""",
                          (paid, req.reason or "cancelled by studio",
                           dt.datetime.now().isoformat(), booking_id))
        refunded = paid

    if req.notify_client and email:
        why = f" ({req.reason})" if req.reason else ""
        times_html = ""
        if req.suggested_times:
            items = "".join(f"<li style='margin:4px 0'>{t}</li>" for t in req.suggested_times[:6])
            times_html = ("<p style='margin:14px 0 6px'><b>Times we have free:</b></p>"
                          f"<ul style='margin:0 0 6px 18px;padding:0;color:#5a4c4e'>{items}</ul>")

        if req.outcome == "refund":
            subject = "Your Rebalance session was cancelled — refund on its way"
            head = "Session cancelled · refund on its way"
            body = (f"<p style='margin:0 0 12px'>Hi {name}, we're sorry — we've had to cancel your "
                    f"session on <b>{req.date} at {req.hour}</b>{why}.</p>"
                    f"<p style='margin:0'>We're refunding <b>€{paid:.2f}</b>. Depending on your bank "
                    "it can take a few days to appear.</p>")
            note = "If you'd rather rebook than take the refund, just call us on 22 035001."
            plain = (f"Hi {name},\n\nWe've cancelled your session on {req.date} at {req.hour}{why}. "
                     f"A refund of EUR {paid:.2f} is on its way.\n\nRebalance, Engomi · 22 035001")
        elif req.outcome == "credit":
            subject = "Your Rebalance session was cancelled — class credit added"
            head = "Session cancelled · credit added"
            body = (f"<p style='margin:0 0 12px'>Hi {name}, we're sorry — we've had to cancel your "
                    f"session on <b>{req.date} at {req.hour}</b>{why}.</p>"
                    "<p style='margin:0'>We've added <b>1 class credit</b> to your account, so you can "
                    "book another time whenever suits you — nothing more to pay.</p>" + times_html)
            note = "Prefer a refund instead? Call us on 22 035001 and we'll sort it."
            plain = (f"Hi {name},\n\nWe've cancelled your session on {req.date} at {req.hour}{why}. "
                     "A class credit has been added to your account.\n\nRebalance, Engomi · 22 035001")
        elif req.outcome == "reschedule":
            subject = "Your Rebalance session — can we move you to another time?"
            head = "Can we move you to another time?"
            body = (f"<p style='margin:0 0 12px'>Hi {name}, we're sorry — we can't run your session on "
                    f"<b>{req.date} at {req.hour}</b>{why}.</p>"
                    "<p style='margin:0'>We'd love to find you another time that works. Sign in to pick "
                    "a new slot, or reply and we'll arrange it for you.</p>" + times_html +
                    ("<p style='margin:12px 0 0'>If none of those suit, just say the word and we'll "
                     f"refund your €{paid:.2f}.</p>" if was_paid else ""))
            note = "Call us on 22 035001 and we'll find something that fits."
            plain = (f"Hi {name},\n\nWe can't run your session on {req.date} at {req.hour}{why}. "
                     "Can we move you to another time? Sign in to rebook, or call 22 035001.\n\n"
                     "Rebalance, Engomi")
        else:
            subject = "Your Rebalance booking was cancelled"
            head = "Your session was cancelled"
            body = (f"<p style='margin:0 0 12px'>Hi {name}, we've had to cancel your session on "
                    f"<b>{req.date} at {req.hour}</b>{why}.</p>"
                    "<p style='margin:0'>Please get in touch and we'll sort it out.</p>")
            note = "Call us on 22 035001."
            plain = (f"Hi {name},\n\nWe've cancelled your session on {req.date} at {req.hour}{why}."
                     "\n\nRebalance, Engomi · 22 035001")

        notify(email, subject, plain, html=_email_html(head, body, footer_note=note))

    return {"ok": True, "cancelled": name, "outcome": req.outcome,
            "refunded": refunded, "credited": credited, "was_paid": was_paid}


# ============================================================================
# VIVA WALLET REFUNDS
#
# HONEST STATUS: this code is written against Viva's documented API but has
# never run against a live account. Before trusting it with real money, test
# it on Viva's DEMO environment first (VIVA_DEMO=1) and confirm a real refund
# appears in the dashboard.
#
# Setup required in the Viva dashboard:
#   Settings -> API Access -> tick "Allow refunds"
#   then set VIVA_CLIENT_ID / VIVA_CLIENT_SECRET in .env
# ============================================================================
VIVA_CLIENT_ID = os.environ.get("VIVA_CLIENT_ID", "")
VIVA_CLIENT_SECRET = os.environ.get("VIVA_CLIENT_SECRET", "")
VIVA_DEMO = os.environ.get("VIVA_DEMO", "1") == "1"     # demo until you say otherwise

_VIVA_HOSTS = {
    True:  {"accounts": "https://demo-accounts.vivapayments.com",
            "api":      "https://demo-api.vivapayments.com"},
    False: {"accounts": "https://accounts.vivapayments.com",
            "api":      "https://api.vivapayments.com"},
}

_viva_token_cache = {"token": None, "expires": 0}


def viva_configured() -> bool:
    return bool(VIVA_CLIENT_ID and VIVA_CLIENT_SECRET)


def _viva_token() -> str:
    """Fetch (and briefly cache) an OAuth token for the Viva API."""
    import urllib.request, urllib.parse, base64, json as _j, time as _t
    if _viva_token_cache["token"] and _viva_token_cache["expires"] > _t.time() + 30:
        return _viva_token_cache["token"]
    host = _VIVA_HOSTS[VIVA_DEMO]["accounts"]
    basic = base64.b64encode(f"{VIVA_CLIENT_ID}:{VIVA_CLIENT_SECRET}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(f"{host}/connect/token", data=data, method="POST")
    req.add_header("Authorization", "Basic " + basic)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = _j.loads(r.read())
    except Exception as e:
        raise HTTPException(502, f"Could not reach Viva to authenticate: {e}")
    tok = body.get("access_token")
    if not tok:
        raise HTTPException(502, "Viva did not return an access token")
    _viva_token_cache["token"] = tok
    _viva_token_cache["expires"] = _t.time() + int(body.get("expires_in", 3600))
    return tok


def viva_refund(transaction_id: str, amount_eur: float, reference: str = "") -> dict:
    """Refund (or same-day cancel) a Viva transaction.
    Amount is in euros; Viva expects cents."""
    import urllib.request, urllib.parse, json as _j
    if not viva_configured():
        raise HTTPException(503, "Viva isn't configured on this server")
    if not transaction_id:
        raise HTTPException(400, "No Viva transaction recorded for this booking")
    host = _VIVA_HOSTS[VIVA_DEMO]["api"]
    cents = int(round(float(amount_eur) * 100))
    params = {"amount": cents}
    if reference:
        params["merchantTrns"] = reference[:100]
    url = f"{host}/checkout/v2/transactions/{transaction_id}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", "Bearer " + _viva_token())
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return _j.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        raise HTTPException(502, f"Viva refused the refund ({e.code}): {detail}")
    except Exception as e:
        raise HTTPException(502, f"Could not reach Viva: {e}")


class RefundReq(BaseModel):
    date: str
    hour: str
    email: str
    amount: float = 0          # 0 = refund the full amount
    reason: str = ""
    manual: bool = False       # True = she already refunded in Viva's dashboard


@app.post("/trainer/refund")
def trainer_refund(req: RefundReq, _=Depends(require_trainer)):
    """Refund a booking. If Viva is configured this calls their API; otherwise
    (or when manual=True) it just records that a refund was made by hand."""
    with db() as c:
        b = c.execute("""SELECT * FROM bookings WHERE user_email=? AND date=? AND hour=?
                         ORDER BY id DESC LIMIT 1""",
                      (req.email, req.date, req.hour)).fetchone()
        if not b:
            raise HTTPException(404, "No booking found for that person and time")
        if b["refund_status"] == "refunded":
            raise HTTPException(409, "That booking has already been refunded")
        paid = float(b["amount"] or 0)
        amount = float(req.amount) if req.amount else paid
        if amount <= 0:
            raise HTTPException(400, "Nothing to refund — that booking was free")
        if amount > paid:
            raise HTTPException(400, f"Can't refund €{amount:.2f} — only €{paid:.2f} was paid")

        via_api = (not req.manual) and viva_configured() and b["viva_txn"]
        result = {}
        if via_api:
            result = viva_refund(b["viva_txn"], amount, f"Rebalance {b['receipt_no'] or ''}")
            status = "refunded"
        else:
            # recorded by hand — she refunded through Viva's dashboard
            status = "refunded"

        c.execute("""UPDATE bookings SET refund_status=?, refund_amount=?, refund_ref=?,
                     refunded_at=? WHERE id=?""",
                  (status, amount, req.reason or ("viva-api" if via_api else "manual"),
                   dt.datetime.now().isoformat(), b["id"]))

    notify(req.email, "Your Rebalance refund",
           f"Hi,\n\nWe've refunded €{amount:.2f} for your session on {req.date} at {req.hour}.\n\n"
           "It can take a few days to appear on your statement, depending on your bank.\n\n"
           "— Rebalance, Engomi",
           html=_email_html(
               "Refund on its way",
               f"<p style='margin:0 0 12px'>We've refunded <b>€{amount:.2f}</b> for your session on "
               f"<b>{req.date} at {req.hour}</b>.</p>"
               "<p style='margin:0'>Depending on your bank it can take a few days to show "
               "on your statement.</p>",
               footer_note="Any questions? Call us on 22 035001."))
    return {"ok": True, "amount": amount, "via": "viva" if via_api else "manual",
            "viva_response": result}


@app.get("/trainer/refunds")
def list_refunds(_=Depends(require_trainer)):
    with db() as c:
        rows = c.execute("""SELECT date,hour,user_email,name,amount,refund_amount,
                            refund_ref,refunded_at,receipt_no FROM bookings
                            WHERE refund_status='refunded' ORDER BY refunded_at DESC""").fetchall()
    return {"refunds": [dict(r) for r in rows]}


@app.get("/viva/status")
def viva_status(_=Depends(require_trainer)):
    """Tells the studio portal whether real refunds are possible yet."""
    return {"configured": viva_configured(), "demo": VIVA_DEMO,
            "note": ("Refunds will be sent to Viva's DEMO environment — no real money moves."
                     if VIVA_DEMO else "LIVE — refunds move real money.")}


def apply_regular_holds(c, date_str: str, hour: str, spots: list) -> list:
    """Reserve a place for anyone with an approved regular slot at this time.
    Their spot is marked as a hold until they book and pay for it."""
    try:
        dow = (dt.date.fromisoformat(date_str).weekday() + 1) % 7   # -> JS Sun=0
    except Exception:
        return spots
    regs = c.execute("""SELECT email, name FROM regular_slots
                        WHERE status='approved' AND weekday=? AND hour=?""",
                     (dow, hour)).fetchall()
    for r in regs:
        # already booked in? nothing to hold
        if any(isinstance(s, dict) and s.get("email") == r["email"] for s in spots):
            continue
        # already holding a place for them?
        if any(isinstance(s, dict) and s.get("regular") and s.get("email") == r["email"]
               for s in spots):
            continue
        free = next((i for i, s in enumerate(spots) if s is None), -1)
        if free >= 0:
            spots[free] = {"name": r["name"], "email": r["email"],
                           "pay": None, "regular": True}
    return spots


@app.get("/trainer/clients")
def trainer_clients(_=Depends(require_trainer)):
    """Every client and their history, from the database — so she sees everyone,
    not just people who happened to book in her own browser."""
    with db() as c:
        users = c.execute("""SELECT id,name,email,phone,profile_photo,credits,id_number,date_of_birth,address,
                              id_status,id_submitted,
                              cliniko_id,cliniko_email,cliniko_synced_at,cliniko_archived,
                              CASE WHEN id_front_path IS NOT NULL THEN 1 ELSE 0 END id_front,
                              CASE WHEN id_back_path IS NOT NULL THEN 1 ELSE 0 END id_back
                              FROM users WHERE email!=?""",
                          (TRAINER_EMAIL,)).fetchall()
        out = []
        for u in users:
            account_email = u["email"]
            bs = c.execute("""SELECT date,hour,class_key,private,pay,amount,status,
                              attended,receipt_no,refund_status
                              FROM bookings WHERE user_email=? ORDER BY date DESC, hour DESC""",
                           (account_email,)).fetchall()
            confirmed = [b for b in bs if b["status"] == "confirmed"]
            spent = sum((b["amount"] or 0) for b in confirmed
                        if b["refund_status"] != "refunded")
            attended = sum(1 for b in confirmed if b["attended"] == 1)
            noshow = sum(1 for b in confirmed if b["attended"] == 0)
            ghs = c.execute("SELECT status FROM ghs_cover WHERE email=?", (account_email,)).fetchone()
            out.append({
                "id": u["id"], "name": u["name"],
                "email": u["cliniko_email"] or account_email, "phone": u["phone"] or "",
                "profile_photo": u["profile_photo"] or "",
                "id_number": u["id_number"] or "", "date_of_birth": u["date_of_birth"] or "",
                "address": u["address"] or "", "id_status": u["id_status"] or "missing",
                "id_submitted": u["id_submitted"] or "", "id_front": bool(u["id_front"]),
                "id_back": bool(u["id_back"]),
                "credits": u["credits"], "sessions": len(confirmed),
                "spent": round(spent, 2), "attended": attended, "noshow": noshow,
                "ghs": ghs["status"] if ghs else None,
                "cliniko_id": u["cliniko_id"] or "",
                "cliniko_synced_at": u["cliniko_synced_at"] or "",
                "archived": bool(u["cliniko_archived"]),
                "bookings": [dict(b) for b in bs[:20]],
            })
        out.sort(key=lambda x: -x["sessions"])
    return {"clients": out}


@app.get("/trainer/cliniko-status")
def trainer_cliniko_status(_=Depends(require_trainer)):
    with db() as c:
        row = c.execute("SELECT v FROM settings WHERE k='cliniko_last_sync'").fetchone()
        linked = c.execute("SELECT COUNT(*) n FROM users WHERE cliniko_id IS NOT NULL AND cliniko_id!=''").fetchone()["n"]
    return {"configured": bool(CLINIKO_API_KEY), "last_sync": row["v"] if row else None,
            "linked_patients": linked, "interval_seconds": CLINIKO_SYNC_SECONDS}


@app.post("/trainer/cliniko-sync")
def trainer_cliniko_sync(_=Depends(require_trainer)):
    if not CLINIKO_API_KEY:
        raise HTTPException(503, "Cliniko is not configured on the server")
    try:
        return sync_cliniko_patients()
    except Exception as exc:
        raise HTTPException(502, f"Cliniko sync failed: {exc}")


@app.get("/trainer/id-document/{user_id}/{side}")
def trainer_id_document(user_id: int, side: str, _=Depends(require_trainer)):
    if side not in ("front", "back"):
        raise HTTPException(404, "ID side not found")
    with db() as c:
        row = c.execute(
            f"SELECT id_{side}_path path,id_{side}_mime mime FROM users WHERE id=?",
            (user_id,)).fetchone()
    if not row or not row["path"] or not os.path.isfile(row["path"]):
        raise HTTPException(404, "ID image not found")
    try:
        with open(row["path"], "rb") as f:
            raw = _id_cipher().decrypt(f.read())
    except Exception:
        raise HTTPException(500, "ID image could not be decrypted")
    return Response(raw, media_type=row["mime"] or "application/octet-stream",
                    headers={"Cache-Control": "no-store, private",
                             "Content-Security-Policy": "default-src 'none'"})


class IdReviewReq(BaseModel):
    user_id: int
    status: str


@app.post("/trainer/id-review")
def trainer_id_review(req: IdReviewReq, _=Depends(require_trainer)):
    if req.status not in ("verified", "rejected", "pending"):
        raise HTTPException(400, "Invalid review status")
    with db() as c:
        cur = c.execute("UPDATE users SET id_status=? WHERE id=?", (req.status, req.user_id))
        if not cur.rowcount:
            raise HTTPException(404, "Customer not found")
    return {"ok": True, "status": req.status}


@app.get("/trainer/earnings-detail")
def trainer_earnings_detail(date_from: str = "", date_to: str = "",
                            _=Depends(require_trainer)):
    """Earnings for a period, with GHS kept separate from money taken at the studio."""
    # include cancelled bookings that were refunded, so the money shows in her records
    q = "SELECT * FROM bookings WHERE (status='confirmed' OR refund_status='refunded')"
    args = []
    if date_from:
        q += " AND date>=?"; args.append(date_from)
    if date_to:
        q += " AND date<=?"; args.append(date_to)
    with db() as c:
        rows = c.execute(q, args).fetchall()
    totals = {"viva": 0.0, "card": 0.0, "cash": 0.0, "package": 0.0, "ghs": 0.0}
    refunded = 0.0
    lines = []
    for r in rows:
        amt = r["amount"] or 0
        if r["refund_status"] == "refunded":
            back = r["refund_amount"] or amt
            refunded += back
            amt = max(0.0, amt - back)
        key = (r["pay"] or "viva").lower()
        if key not in totals:
            key = "viva"
        totals[key] += amt
        lines.append({"date": r["date"], "hour": r["hour"], "client": r["name"],
                      "class": CLASSES.get(r["class_key"], {}).get("name", r["class_key"]),
                      "private": bool(r["private"]), "pay": r["pay"],
                      "amount": amt, "receipt": r["receipt_no"],
                      "refunded": r["refund_status"] == "refunded"})
    taken = totals["viva"] + totals["card"] + totals["cash"]
    return {"taken_at_studio": round(taken, 2),
            "to_claim_from_ghs": round(totals["ghs"], 2),
            "total_earned": round(taken + totals["ghs"], 2),
            "viva": round(totals["viva"], 2), "card": round(totals["card"], 2),
            "cash": round(totals["cash"], 2), "package": round(totals["package"], 2),
            "refunded": round(refunded, 2),
            "sessions": sum(1 for r in rows if r["status"] == "confirmed"),
            "lines": sorted(lines, key=lambda x: (x["date"], x["hour"]), reverse=True)}


@app.get("/calendar/status")
def calendar_status(_=Depends(require_trainer)):
    """Whether Google Calendar sync is switched on, and what's missing if not."""
    have_id = bool(CALENDAR_ID)
    have_creds = bool(GOOGLE_CREDENTIALS_FILE and os.path.exists(GOOGLE_CREDENTIALS_FILE))
    missing = []
    if not have_id:
        missing.append("CALENDAR_ID")
    if not have_creds:
        missing.append("GOOGLE_CREDENTIALS_FILE (service account JSON)")
    try:
        import googleapiclient  # noqa
        have_lib = True
    except Exception:
        have_lib = False
        missing.append("google-api-python-client (pip install)")
    return {"enabled": have_id and have_creds and have_lib, "missing": missing,
            "calendar_id": CALENDAR_ID or None}


class AttendReq(BaseModel):
    date: str
    hour: str
    spot: int
    present: bool = True


@app.post("/trainer/attendance")
def trainer_attendance(req: AttendReq, _=Depends(require_trainer)):
    """Mark a client present or a no-show. Stored on the booking so it appears
    in their history and in the client list."""
    with db() as c:
        slot, spots = get_or_make_slot(c, req.date, req.hour)
        if not (0 <= req.spot < len(spots)) or not isinstance(spots[req.spot], dict):
            raise HTTPException(404, "That spot isn't booked")
        spots[req.spot]["attended"] = bool(req.present)
        save_slot(c, slot, spots)
        email = spots[req.spot].get("email")
        if email:
            c.execute("""UPDATE bookings SET attended=? WHERE user_email=? AND date=? AND hour=?
                         AND status='confirmed'""",
                      (1 if req.present else 0, email, req.date, req.hour))
    return {"ok": True, "present": req.present}


@app.get("/trainer/free-times")
def trainer_free_times(date: str, class_key: str = "", days: int = 7,
                       _=Depends(require_trainer)):
    """Free slots over the next few days — used to offer a customer alternatives
    when their session has to be cancelled."""
    out = []
    try:
        start = dt.date.fromisoformat(date)
    except Exception:
        start = dt.date.today()
    with db() as c:
        for i in range(max(1, min(days, 21))):
            d = (start + dt.timedelta(days=i)).isoformat()
            for hour in CLASS_HOURS:
                if is_closed(c, d, hour):
                    continue
                row = c.execute("SELECT * FROM slots WHERE date=? AND hour=?", (d, hour)).fetchone()
                if row:
                    if row["closed"] or row["private"]:
                        continue
                    if class_key and row["class_key"] != class_key:
                        continue
                    spots = json.loads(row["spots"])
                    free = sum(1 for s in spots if s is None)
                    ck = row["class_key"]
                else:
                    idx = CLASS_HOURS.index(hour)
                    ck = list(CLASSES.keys())[idx % len(CLASSES)]
                    if class_key and ck != class_key:
                        continue
                    free = CAP
                if free > 0:
                    out.append({"date": d, "hour": hour, "class_key": ck,
                                "class": CLASSES[ck]["name"], "free": free,
                                "label": f"{d} at {hour} — {CLASSES[ck]['name']}"})
    return {"times": out[:20]}


# ============================================================================
# PASSKEYS (WebAuthn)
#
# Lets people sign in with Face ID / Touch ID / Windows Hello / a security key
# instead of a password. The private key never leaves their device; we only
# store the public key and verify signatures.
#
# RP_ID must be the site's domain (no https://, no port). For
# rebalance.solonstudios.org that's exactly that. Set PASSKEY_RP_ID in .env if
# the domain differs.
# ============================================================================
import base64 as _b64

PASSKEY_RP_ID = os.environ.get("PASSKEY_RP_ID", "rebalance.solonstudios.org")
PASSKEY_RP_NAME = "Rebalance"
PASSKEY_ORIGIN = os.environ.get("PASSKEY_ORIGIN", f"https://{PASSKEY_RP_ID}")


def _pk_import():
    from webauthn import (generate_registration_options, verify_registration_response,
                          generate_authentication_options, verify_authentication_response,
                          options_to_json)
    from webauthn.helpers.structs import (PublicKeyCredentialDescriptor,
                                          AuthenticatorSelectionCriteria, ResidentKeyRequirement,
                                          UserVerificationRequirement)
    return locals()


def _save_challenge(c, email, challenge, kind):
    c.execute("""INSERT INTO webauthn_challenges(email,challenge,kind,created)
                 VALUES(?,?,?,?)
                 ON CONFLICT(email) DO UPDATE SET challenge=?, kind=?, created=?""",
              (email, challenge, kind, dt.datetime.now().isoformat(),
               challenge, kind, dt.datetime.now().isoformat()))


def _get_challenge(c, email, kind):
    row = c.execute("SELECT challenge FROM webauthn_challenges WHERE email=? AND kind=?",
                    (email, kind)).fetchone()
    return row["challenge"] if row else None


@app.get("/passkey/config")
def passkey_config():
    """The site checks this to decide whether to show passkey options."""
    try:
        import webauthn  # noqa
        return {"enabled": True, "rp_id": PASSKEY_RP_ID}
    except Exception:
        return {"enabled": False, "rp_id": PASSKEY_RP_ID}


class PkRegBegin(BaseModel):
    pass


@app.post("/passkey/register/begin")
def passkey_register_begin(user=Depends(current_user)):
    """Start adding a passkey to the signed-in account."""
    pk = _pk_import()
    with db() as c:
        existing = c.execute("SELECT credential_id FROM passkeys WHERE email=?",
                             (user["email"],)).fetchall()
    exclude = [pk["PublicKeyCredentialDescriptor"](id=_b64.urlsafe_b64decode(r["credential_id"] + "=="))
               for r in existing]
    opts = pk["generate_registration_options"](
        rp_id=PASSKEY_RP_ID, rp_name=PASSKEY_RP_NAME,
        user_name=user["email"], user_display_name=user["name"],
        exclude_credentials=exclude,
        authenticator_selection=pk["AuthenticatorSelectionCriteria"](
            resident_key=pk["ResidentKeyRequirement"].PREFERRED,
            user_verification=pk["UserVerificationRequirement"].PREFERRED),
    )
    challenge_b64 = _b64.urlsafe_b64encode(opts.challenge).decode().rstrip("=")
    with db() as c:
        _save_challenge(c, user["email"], challenge_b64, "register")
    return json.loads(pk["options_to_json"](opts))


class PkRegFinish(BaseModel):
    credential: dict
    device_name: str = ""


@app.post("/passkey/register/finish")
def passkey_register_finish(req: PkRegFinish, user=Depends(current_user)):
    pk = _pk_import()
    with db() as c:
        ch = _get_challenge(c, user["email"], "register")
    if not ch:
        raise HTTPException(400, "No pending passkey registration — start again")
    try:
        verification = pk["verify_registration_response"](
            credential=req.credential,
            expected_challenge=_b64.urlsafe_b64decode(ch + "=="),
            expected_rp_id=PASSKEY_RP_ID,
            expected_origin=PASSKEY_ORIGIN,
        )
    except Exception as e:
        raise HTTPException(400, f"Could not verify that passkey: {e}")
    cred_id = _b64.urlsafe_b64encode(verification.credential_id).decode().rstrip("=")
    pub = _b64.urlsafe_b64encode(verification.credential_public_key).decode().rstrip("=")
    with db() as c:
        c.execute("""INSERT OR REPLACE INTO passkeys(email,credential_id,public_key,sign_count,name,created)
                     VALUES(?,?,?,?,?,?)""",
                  (user["email"], cred_id, pub, verification.sign_count,
                   req.device_name or "My device", dt.datetime.now().isoformat()))
        c.execute("DELETE FROM webauthn_challenges WHERE email=?", (user["email"],))
    return {"ok": True}


class PkAuthBegin(BaseModel):
    email: str


@app.post("/passkey/login/begin")
def passkey_login_begin(req: PkAuthBegin):
    pk = _pk_import()
    email = req.email.lower().strip()
    with db() as c:
        keys = c.execute("SELECT credential_id FROM passkeys WHERE email=?", (email,)).fetchall()
    if not keys:
        raise HTTPException(404, "NO_PASSKEY")
    allow = [pk["PublicKeyCredentialDescriptor"](id=_b64.urlsafe_b64decode(k["credential_id"] + "=="))
             for k in keys]
    opts = pk["generate_authentication_options"](
        rp_id=PASSKEY_RP_ID, allow_credentials=allow,
        user_verification=pk["UserVerificationRequirement"].PREFERRED)
    challenge_b64 = _b64.urlsafe_b64encode(opts.challenge).decode().rstrip("=")
    with db() as c:
        _save_challenge(c, email, challenge_b64, "login")
    return json.loads(pk["options_to_json"](opts))


class PkAuthFinish(BaseModel):
    email: str
    credential: dict
    remember: bool = True


@app.post("/passkey/login/finish")
def passkey_login_finish(req: PkAuthFinish):
    pk = _pk_import()
    email = req.email.lower().strip()
    raw_id = req.credential.get("rawId") or req.credential.get("id")
    with db() as c:
        ch = _get_challenge(c, email, "login")
        if not ch:
            raise HTTPException(400, "No pending sign-in — start again")
        # find the stored key that matches
        row = None
        for k in c.execute("SELECT * FROM passkeys WHERE email=?", (email,)).fetchall():
            row = k
            try:
                if _b64.urlsafe_b64decode(k["credential_id"] + "==") == _b64.urlsafe_b64decode(raw_id + "=="):
                    row = k
                    break
            except Exception:
                continue
        if not row:
            raise HTTPException(404, "That passkey isn't registered")
        try:
            verification = pk["verify_authentication_response"](
                credential=req.credential,
                expected_challenge=_b64.urlsafe_b64decode(ch + "=="),
                expected_rp_id=PASSKEY_RP_ID,
                expected_origin=PASSKEY_ORIGIN,
                credential_public_key=_b64.urlsafe_b64decode(row["public_key"] + "=="),
                credential_current_sign_count=row["sign_count"],
            )
        except Exception as e:
            raise HTTPException(401, f"Passkey sign-in failed: {e}")
        # bump the counter (guards against cloned authenticators)
        c.execute("UPDATE passkeys SET sign_count=? WHERE id=?",
                  (verification.new_sign_count, row["id"]))
        c.execute("DELETE FROM webauthn_challenges WHERE email=?", (email,))
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u:
            raise HTTPException(404, "NO_ACCOUNT")
        token = make_session(c, email, "customer", req.remember)
        c.execute("UPDATE users SET token=? WHERE id=?", (token, u["id"]))
    return {"ok": True, "token": token, "name": u["name"], "email": email,
            "credits": u["credits"]}


@app.get("/passkey/list")
def passkey_list(user=Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT id,name,created FROM passkeys WHERE email=?",
                        (user["email"],)).fetchall()
    return {"passkeys": [dict(r) for r in rows]}


@app.delete("/passkey/{pid}")
def passkey_delete(pid: int, user=Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM passkeys WHERE id=? AND email=?", (pid, user["email"]))
    return {"ok": True}


class ContactReq(BaseModel):
    name: str
    email: str
    message: str
    phone: str = ""


@app.post("/contact")
def contact(req: ContactReq, request: Request):
    """A message from the public contact form — emailed to the studio."""
    rate_limit("contact:" + client_ip(request), limit=5, window=300)
    name = req.name.strip()[:120]
    email = req.email.strip()[:160]
    msg = req.message.strip()[:4000]
    if not (name and email and msg):
        raise HTTPException(400, "Please fill in your name, email and message")
    if "@" not in email:
        raise HTTPException(400, "That email doesn't look right")
    phone = req.phone.strip()[:40]
    body = (f"New enquiry from the website:\n\n"
            f"Name: {name}\nEmail: {email}\n"
            + (f"Phone: {phone}\n" if phone else "")
            + f"\nMessage:\n{msg}\n")
    # send to the studio, with the customer as reply-to so she can just hit reply
    notify(TRAINER_EMAIL, f"Website enquiry from {name}", body,
           html=_email_html(
               "New website enquiry",
               f"<table cellpadding='0' cellspacing='0' style='width:100%;font-size:14px;color:#5a4c4e'>"
               f"<tr><td style='padding:8px 0;color:#8a7a7c'>Name</td>"
               f"<td align='right' style='padding:8px 0;font-weight:bold;color:#4a3b3e'>{name}</td></tr>"
               f"<tr><td style='padding:8px 0;color:#8a7a7c'>Email</td>"
               f"<td align='right' style='padding:8px 0;color:#4a3b3e'>{email}</td></tr>"
               + (f"<tr><td style='padding:8px 0;color:#8a7a7c'>Phone</td>"
                  f"<td align='right' style='padding:8px 0;color:#4a3b3e'>{phone}</td></tr>" if phone else "")
               + f"</table><p style='margin:16px 0 6px;color:#8a7a7c'>Message:</p>"
               f"<p style='margin:0;color:#4a3b3e;line-height:1.6'>{msg}</p>",
               footer_note=f"Reply straight to this email to answer {name}."),
           reply_to=email)
    return {"ok": True}
