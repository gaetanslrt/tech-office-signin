"""
Tech office sign-in — Flask backend.

Students pick Arriving or Leaving at /        (no password)
Students fill in the sign-in form at /signin  (no password)
Students sign out when leaving at    /signout (student ID only)
Mr. Jeter reads the log at          /admin    (password from the environment)

Environment variables
---------------------
ADMIN_PASSWORD  required  the password for /admin
SECRET_KEY      required  random string that signs the admin cookie
DATABASE_URL    optional  Postgres URL; without it the app writes a SQLite file
TZ_NAME         optional  display timezone, default America/New_York
SESSION_HOURS   optional  how long a staff login lasts, default 8

Run locally:  python app.py
On Render:    gunicorn app:app --bind 0.0.0.0:$PORT
"""

from __future__ import annotations

import csv
import hmac
import io
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    jsonify,
    request,
    send_from_directory,
    session,
)

import storage

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
TZ_NAME = os.environ.get("TZ_NAME", "America/New_York")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "8"))
IS_PRODUCTION = bool(os.environ.get("RENDER") or os.environ.get("PRODUCTION"))

if not ADMIN_PASSWORD:
    raise RuntimeError(
        "ADMIN_PASSWORD is not set. Put it in your .env file locally, or in the "
        "Environment tab of your Render service."
    )

if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError(
            "SECRET_KEY is not set. Without it, every restart logs the admin out."
        )
    SECRET_KEY = secrets.token_urlsafe(32)
    print("[warn] SECRET_KEY not set — using a temporary one for this run only.")

try:
    DISPLAY_TZ = ZoneInfo(TZ_NAME)
except Exception:
    print(f"[warn] Unknown TZ_NAME {TZ_NAME!r}; falling back to UTC.")
    DISPLAY_TZ = timezone.utc

# The nine reasons, exactly as they appear in the dropdown.
REASONS = [
    "Bezel/Frame",
    "Headphone/Audio",
    "Keyboard",
    "Power",
    "Repair/Other",
    "Screen",
    "Webcam/Microphone",
    "WiFi/Network",
    "Software/Application",
]
REASON_SET = set(REASONS)

MAX_NAME_LEN = 60

# Login throttling. Held in memory, so it resets when the service restarts —
# enough to stop someone typing guesses, not a substitute for a long password.
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW = 900     # 15 minutes
LOCKOUT_SECONDS = 900    # 15 minutes
_attempts: dict[str, list[float]] = defaultdict(list)
_locked_until: dict[str, float] = {}


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    JSON_SORT_KEYS=False,
    MAX_CONTENT_LENGTH=64 * 1024,
)

storage.init()
print(f"[ok] Sign-in log ready — storage backend: {storage.backend_name()}")


@app.after_request
def _security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.get("/")
def kiosk():
    return send_from_directory(STATIC_DIR, "home.html")


@app.get("/signin")
def signin_page():
    return send_from_directory(STATIC_DIR, "signin.html")


@app.get("/signout")
def signout_page():
    return send_from_directory(STATIC_DIR, "signout.html")


@app.get("/admin")
def admin_page():
    return send_from_directory(STATIC_DIR, "admin.html")


@app.get("/healthz")
def healthz():
    """Hit by an uptime pinger to keep a free Render service awake."""
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# Student form
# --------------------------------------------------------------------------

def _clean(value: object) -> str:
    """Collapse whitespace so ' Jean   Luc ' is stored as 'Jean Luc'."""
    return " ".join(str(value or "").split())


@app.get("/api/reasons")
def reasons():
    """The dropdown can read its options from here, so the list lives in one place."""
    return jsonify(reasons=REASONS)


@app.post("/api/visits")
def create_visit():
    data = request.get_json(silent=True) or {}

    first_name = _clean(data.get("first_name"))
    last_name = _clean(data.get("last_name"))
    student_id = _clean(data.get("student_id"))
    reason = _clean(data.get("reason"))

    errors: dict[str, str] = {}
    if not first_name:
        errors["first_name"] = "Enter your first name."
    elif len(first_name) > MAX_NAME_LEN:
        errors["first_name"] = "That name is too long."

    if not last_name:
        errors["last_name"] = "Enter your last name."
    elif len(last_name) > MAX_NAME_LEN:
        errors["last_name"] = "That name is too long."

    if not student_id:
        errors["student_id"] = "Enter your student ID."
    elif len(student_id) > MAX_NAME_LEN:
        errors["student_id"] = "That ID is too long."

    if reason not in REASON_SET:
        errors["reason"] = "Pick a reason from the list."

    if errors:
        return jsonify(ok=False, errors=errors), 422

    row = storage.add_visit(first_name, last_name, student_id, reason)
    local = datetime.fromisoformat(row["created_at"]).astimezone(DISPLAY_TZ)

    return jsonify(
        ok=True,
        first_name=row["first_name"],
        recorded_at=_fmt_time(local),
    )


@app.post("/api/signout")
def sign_out():
    """Fill in the time out on today's open visit for this student ID."""
    data = request.get_json(silent=True) or {}
    student_id = _clean(data.get("student_id"))

    if not student_id:
        return jsonify(ok=False, errors={"student_id": "Enter your student ID."}), 422
    if len(student_id) > MAX_NAME_LEN:
        return jsonify(ok=False, errors={"student_id": "That ID is too long."}), 422

    # Only today's visits, so a sign-out forgotten yesterday is not closed
    # with today's time.
    midnight = datetime.now(DISPLAY_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    row = storage.sign_out(student_id, since=midnight)
    if row is None:
        return jsonify(
            ok=False,
            error="We couldn't find a sign-in for that ID today. Check the number, "
                  "or tell Mr. Jeter.",
        ), 404

    arrived = datetime.fromisoformat(row["created_at"]).astimezone(DISPLAY_TZ)
    left = datetime.fromisoformat(row["left_at"]).astimezone(DISPLAY_TZ)
    return jsonify(
        ok=True,
        first_name=row["first_name"],
        arrived_at=_fmt_time(arrived),
        left_at=_fmt_time(left),
    )


# --------------------------------------------------------------------------
# Admin authentication
# --------------------------------------------------------------------------

def _client_key() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _locked_seconds(key: str) -> int:
    remaining = int(_locked_until.get(key, 0) - time.time())
    return remaining if remaining > 0 else 0


def _record_failure(key: str) -> None:
    now = time.time()
    hits = [t for t in _attempts[key] if now - t < ATTEMPT_WINDOW]
    hits.append(now)
    _attempts[key] = hits
    if len(hits) >= MAX_ATTEMPTS:
        _locked_until[key] = now + LOCKOUT_SECONDS
        _attempts[key] = []


def is_admin() -> bool:
    return bool(session.get("admin"))


@app.post("/api/login")
def login():
    key = _client_key()

    locked = _locked_seconds(key)
    if locked:
        minutes = max(1, round(locked / 60))
        return jsonify(ok=False, error=f"Too many attempts. Try again in {minutes} min."), 429

    data = request.get_json(silent=True) or {}
    supplied = str(data.get("password", ""))

    # compare_digest keeps the check from leaking the password through timing.
    if not hmac.compare_digest(supplied, ADMIN_PASSWORD):
        _record_failure(key)
        return jsonify(ok=False, error="Wrong password."), 401

    _attempts.pop(key, None)
    _locked_until.pop(key, None)

    session.clear()
    session.permanent = True
    session["admin"] = True
    session["since"] = int(time.time())
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/session")
def session_state():
    return jsonify(authenticated=is_admin())


# --------------------------------------------------------------------------
# Admin data
# --------------------------------------------------------------------------

def _fmt_time(dt: datetime) -> str:
    """'9:05 AM' without a leading zero, on Linux and Windows alike."""
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {dt.strftime('%p')}"


def _fmt_date(dt: datetime) -> str:
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _rows_for_display() -> list[dict]:
    out = []
    for r in storage.list_visits():
        local = datetime.fromisoformat(r["created_at"]).astimezone(DISPLAY_TZ)
        left = (
            datetime.fromisoformat(r["left_at"]).astimezone(DISPLAY_TZ)
            if r.get("left_at") else None
        )
        out.append(
            {
                "id": r["id"],
                "date": _fmt_date(local),
                "time": _fmt_time(local),
                "iso": local.isoformat(),
                "first_name": r["first_name"],
                "last_name": r["last_name"],
                "student_id": r["student_id"],
                "reason": r["reason"],
                "time_out": _fmt_time(left) if left else "",
            }
        )
    return out


@app.get("/api/visits")
def read_visits():
    if not is_admin():
        return jsonify(ok=False, error="Not signed in."), 401
    return jsonify(ok=True, timezone=TZ_NAME, visits=_rows_for_display())


# The admin must type this word before the whole log is deleted.
CLEAR_CONFIRMATION = "DELETE"


@app.delete("/api/visits")
def clear_visits():
    if not is_admin():
        return jsonify(ok=False, error="Not signed in."), 401

    data = request.get_json(silent=True) or {}
    if str(data.get("confirm", "")).strip() != CLEAR_CONFIRMATION:
        return jsonify(ok=False, error=f"Type {CLEAR_CONFIRMATION} to confirm."), 422

    deleted = storage.clear_visits()
    return jsonify(ok=True, deleted=deleted)


@app.get("/api/visits.csv")
def export_csv():
    """The whole log, or one student's visits with ?student_id=..."""
    if not is_admin():
        return jsonify(ok=False, error="Not signed in."), 401

    rows = _rows_for_display()
    student_id = _clean(request.args.get("student_id"))
    if student_id:
        wanted = student_id.lower()
        rows = [r for r in rows if r["student_id"].lower() == wanted]
        if not rows:
            return jsonify(ok=False, error=f"No visits for student ID {student_id}."), 404

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Date", "Time in", "Time out", "Last name", "First name", "Student ID", "Reason"]
    )
    for r in rows:
        writer.writerow([
            r["date"], r["time"], r["time_out"], r["last_name"],
            r["first_name"], r["student_id"], r["reason"],
        ])

    stamp = datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d")
    filename = f"tech-office-signin-{stamp}.csv"
    if student_id:
        # Keep only filename-safe characters from the ID.
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", student_id)[:30] or "student"
        filename = f"tech-office-signin-{safe_id}-{stamp}.csv"

    # utf-8-sig so Excel on Windows opens accented names correctly.
    payload = buffer.getvalue().encode("utf-8-sig")
    return Response(
        payload,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="127.0.0.1", port=port, debug=False)
