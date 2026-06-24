"""
Simple Event + DJ + Guest Management App with optional Google Sheets sync.

How to run locally:
1. Install requirements:
   python -m pip install -r requirements.txt
2. Run:
   python app.py
3. Open:
   http://127.0.0.1:5000

Local data is stored in data.json.

Google Sheets sync:
- Copy google_sheets_config.example.json to google_sheets_config.json
- Add your spreadsheet_id
- Add a Google service account JSON file named service_account.json
- Share the Google Sheet with the service account email
- Restart the app

The app will then dynamically update Google Sheets whenever:
- an event is created
- an event is deleted
- a guest is added
- a guest receives an SMS confirmation request
- a guest replies Y/N by SMS
- a guest is removed
- a guest is checked in
- a guest is unchecked
"""

from flask import Flask, jsonify, request, render_template, redirect, session, url_for, Response
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash
import csv
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-this-secret-key")

DATA_FILE = Path("data.json")
GOOGLE_SHEETS_CONFIG_FILE = Path("google_sheets_config.json")
SERVICE_ACCOUNT_FILE = Path("service_account.json")
TEAM_MEMBERS_FILE = Path("team_members.txt")
AUTH_DB_FILE = Path("auth.sqlite3")
PIN_TOKEN_HOURS = 1
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
SMS_CONFIRMATION_MESSAGE = "REPLY Y to confirm attendance and N to cancel"
SMS_Y_REPLY_MESSAGE = "Thank you for confirming. You are checked in and we’ll see you there."
SMS_N_REPLY_MESSAGE = "Sorry to hear you can’t make it. We’re disappointed to miss you, but thanks for letting us know."


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_team_member_records():
    return [
        {"name": "Ryan Raitz", "email": ""},
        {"name": "Abby Sparks", "email": ""},
        {"name": "Ty Abbott", "email": ""},
        {"name": "Bil Carter", "email": ""},
    ]


def parse_team_member_line(line):
    """
    Supports either:
      Name
      Name,email@example.com

    The secure PIN setup flow requires the email form. Lines with names only
    still appear in the dropdown, but those users cannot create a PIN until an
    approved email is added beside their name.
    """
    cleaned = line.strip()
    if not cleaned or cleaned.startswith("#"):
        return None

    if "," in cleaned:
        name, email = cleaned.split(",", 1)
        return {"name": name.strip(), "email": email.strip().lower()}

    return {"name": cleaned, "email": ""}


def load_team_member_records():
    if not TEAM_MEMBERS_FILE.exists():
        lines = [f'{record["name"]},{record["email"]}' for record in default_team_member_records()]
        TEAM_MEMBERS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records = []
    seen_names = set()
    for line in TEAM_MEMBERS_FILE.read_text(encoding="utf-8").splitlines():
        record = parse_team_member_line(line)
        if not record:
            continue

        name = record["name"]
        if name and name not in seen_names:
            records.append(record)
            seen_names.add(name)

    return records


def load_team_members():
    return [record["name"] for record in load_team_member_records()]


def get_auth_connection():
    connection = sqlite3.connect(AUTH_DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_auth_db():
    with get_auth_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS team_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                email TEXT,
                pin_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS pin_setup_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(member_id) REFERENCES team_members(id)
            )
        """)

        now = utc_now_iso()
        for record in load_team_member_records():
            connection.execute(
                "INSERT OR IGNORE INTO team_members (name, email, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (record["name"], record["email"], now, now)
            )
            if record["email"]:
                connection.execute(
                    "UPDATE team_members SET email = ?, updated_at = ? WHERE name = ?",
                    (record["email"], now, record["name"])
                )


def hash_token(token):
    return generate_password_hash(token)


def token_matches(stored_hash, token):
    return check_password_hash(stored_hash, token)


def get_member_by_name(name):
    with get_auth_connection() as connection:
        return connection.execute(
            "SELECT * FROM team_members WHERE name = ?",
            (name,)
        ).fetchone()


def get_current_member():
    member_id = session.get("member_id")
    if not member_id:
        return None
    with get_auth_connection() as connection:
        return connection.execute(
            "SELECT id, name, email FROM team_members WHERE id = ?",
            (member_id,)
        ).fetchone()


def get_current_member_name():
    """Return the logged-in team member name for audit columns/edits."""
    return session.get("member_name") or ""


def is_valid_email(email):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def is_valid_phone(phone):
    """Accept common formatting, but require exactly 10 digits."""
    digits = re.sub(r"\D", "", phone or "")
    return len(digits) == 10


def contact_validation_errors(phone, email):
    errors = []

    if not phone and not email:
        errors.append("Add at least one contact method: phone number or email.")

    if phone and not is_valid_phone(phone):
        errors.append("Phone number must be exactly 10 digits. Example: 555-555-5555.")

    if email and not is_valid_email(email):
        errors.append("Email must be formatted correctly. Example: guest@email.com.")

    return errors



def normalize_phone_digits(phone):
    """Normalize phone numbers to 10 US digits for matching inbound replies."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def format_phone_for_sms(phone):
    """Return an E.164-ish US number for Twilio, or blank when invalid."""
    digits = normalize_phone_digits(phone)
    if len(digits) != 10:
        return ""
    return f"+1{digits}"


def sms_is_configured():
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER)


def send_sms_message(to_phone, message):
    """
    Sends an SMS through Twilio when credentials are configured.
    If Twilio is not configured or the phone is invalid, returns a non-fatal status.
    """
    to_number = format_phone_for_sms(to_phone)
    if not to_number:
        return {"sent": False, "message": "No valid phone number available for SMS."}

    if not sms_is_configured():
        print(f"\nSMS NOT SENT — Twilio is not configured. To: {to_number}. Body: {message}\n")
        return {"sent": False, "message": "Twilio is not configured. SMS was printed in the terminal."}

    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        twilio_message = client.messages.create(
            body=message,
            from_=TWILIO_FROM_NUMBER,
            to=to_number,
        )
        return {"sent": True, "message": "SMS sent successfully.", "sid": twilio_message.sid}
    except Exception as error:
        print(f"\nSMS SEND FAILED: {error}\n")
        return {"sent": False, "message": f"SMS send failed: {error}"}


def send_guest_confirmation_sms(guest):
    """Send the one-time Y/N confirmation request for a newly added guest."""
    if guest.get("smsConfirmationSent"):
        return {"sent": False, "message": "Confirmation SMS was already sent for this guest."}

    result = send_sms_message(guest.get("phone", ""), SMS_CONFIRMATION_MESSAGE)
    guest["smsConfirmationSent"] = bool(result.get("sent"))
    guest["smsConfirmationSentAt"] = utc_now_iso() if result.get("sent") else ""
    guest["smsConfirmationStatus"] = result.get("message", "")
    return result


def find_pending_guest_by_phone(data, inbound_phone):
    """
    Match an inbound SMS reply to the newest pending guest with that phone number.
    This lets the same person be re-added later without old replies changing old rows.
    """
    inbound_digits = normalize_phone_digits(inbound_phone)
    if not inbound_digits:
        return None

    matches = [
        guest for guest in data.get("guests", [])
        if normalize_phone_digits(guest.get("phone", "")) == inbound_digits
        and str(guest.get("text", "Pending")).upper() == "PENDING"
    ]
    return matches[-1] if matches else None

def parse_friends_count(value):
    """Return an integer friends count, using 0 when the field is blank/missing."""
    if value is None:
        return 0

    cleaned = str(value).strip()
    if not cleaned:
        return 0

    if not cleaned.isdigit():
        raise ValueError("Friends must be a whole number.")

    return int(cleaned)


def send_pin_setup_email(email, name, setup_url):
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    from_email = os.environ.get("FROM_EMAIL", smtp_user or "no-reply@cliquecabana.local")

    subject = "Create your Clique Cabana PIN"
    body = f"""Hi {name},

Use this one-time link to create your Clique Cabana guest list PIN:

{setup_url}

This link expires in {PIN_TOKEN_HOURS} hour. If you did not request this, ignore this email.
"""

    if not smtp_host or not smtp_user or not smtp_password:
        print("\nPIN SETUP EMAIL NOT SENT — SMTP is not configured.")
        print(f"Recipient: {email}")
        print(f"Setup link: {setup_url}\n")
        return {"sent": False, "message": "SMTP is not configured. The setup link was printed in the terminal.", "setupUrl": setup_url}

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = email
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(message)

    return {"sent": True, "message": "Setup email sent."}


def default_data():
    return {
        "events": [
            {
                "id": "event_001",
                "name": "Opening Night",
                "djs": ["DJ Nova", "Harny", "Wave Runner"],
                "teamMembers": []
            },
            {
                "id": "event_002",
                "name": "After Hours Session",
                "djs": ["DJ Luna", "Midnight Mike"],
                "teamMembers": []
            }
        ],
        "guests": []
    }


def normalize_data(data):
    """Keep older data.json files compatible with newer fields."""
    data.setdefault("events", [])
    data.setdefault("guests", [])

    event_lookup = {event.get("id"): event for event in data["events"]}

    for event in data["events"]:
        event.setdefault("djs", [])
        event.setdefault("teamMembers", [])

    for guest in data["guests"]:
        guest.setdefault("teamMember", "")
        guest.setdefault("text", "Pending")
        guest.setdefault("smsConfirmationSent", False)
        guest.setdefault("smsConfirmationSentAt", "")
        guest.setdefault("smsConfirmationStatus", "")

    return data


def load_data():
    if not DATA_FILE.exists():
        save_data(default_data())
    with DATA_FILE.open("r", encoding="utf-8") as file:
        return normalize_data(json.load(file))


def save_data(data):
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


def slugify_sheet_title(value):
    """
    Google Sheet tab names cannot contain: : \ / ? * [ ]
    They also need to be 100 chars or fewer.
    """
    cleaned = re.sub(r"[:\\\/\?\*\[\]]", "-", value).strip()
    cleaned = cleaned[:90] if cleaned else "Untitled Event"
    return cleaned


def get_sheets_config():
    if not GOOGLE_SHEETS_CONFIG_FILE.exists():
        return None

    with GOOGLE_SHEETS_CONFIG_FILE.open("r", encoding="utf-8") as file:
        config = json.load(file)

    if not config.get("enabled"):
        return None

    if not config.get("spreadsheet_id"):
        return None

    if not SERVICE_ACCOUNT_FILE.exists():
        return None

    return config


def sync_google_sheets():
    """
    Optional sync.

    This rewrites the Google Sheet to match the local data exactly:
    - Creates one worksheet/tab per event
    - Adds one Index worksheet
    - Removes old event worksheets that no longer correspond to current events
    - Writes current guest rows for each event
    """
    config = get_sheets_config()

    if not config:
        return {"enabled": False, "message": "Google Sheets sync is not configured."}

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        return {
            "enabled": False,
            "message": "Google Sheets packages are not installed. Run: python -m pip install -r requirements.txt"
        }

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_file(str(SERVICE_ACCOUNT_FILE), scopes=scopes)
    client = gspread.authorize(credentials)

    spreadsheet = client.open_by_key(config["spreadsheet_id"])
    data = load_data()

    index_title = config.get("index_sheet_name", "Event Index")
    protected_tabs = set(config.get("protected_tabs", []))
    protected_tabs.add(index_title)

    event_sheet_titles = {}
    used_titles = set(protected_tabs)

    for event in data["events"]:
        base_title = slugify_sheet_title(event["name"])
        title = base_title
        counter = 2

        while title in used_titles:
            suffix = f" {counter}"
            title = f"{base_title[:90 - len(suffix)]}{suffix}"
            counter += 1

        used_titles.add(title)
        event_sheet_titles[event["id"]] = title

    existing_worksheets = {worksheet.title: worksheet for worksheet in spreadsheet.worksheets()}

    if index_title in existing_worksheets:
        index_sheet = existing_worksheets[index_title]
        index_sheet.clear()
    else:
        index_sheet = spreadsheet.add_worksheet(title=index_title, rows=100, cols=8)

    index_rows = [
        ["Event Name", "DJs", "Total Guests", "Checked In", "Not Checked In", "Sheet Tab"]
    ]

    for event in data["events"]:
        event_guests = [guest for guest in data["guests"] if guest["eventId"] == event["id"]]
        checked_in_count = sum(1 for guest in event_guests if guest.get("checkedIn"))

        index_rows.append([
            event["name"],
            ", ".join(event["djs"]),
            len(event_guests),
            checked_in_count,
            len(event_guests) - checked_in_count,
            event_sheet_titles[event["id"]]
        ])

    index_sheet.update(index_rows)

    desired_event_titles = set(event_sheet_titles.values())

    for title, worksheet in list(existing_worksheets.items()):
        if title not in protected_tabs and title not in desired_event_titles:
            spreadsheet.del_worksheet(worksheet)

    existing_worksheets = {worksheet.title: worksheet for worksheet in spreadsheet.worksheets()}

    for event in data["events"]:
        title = event_sheet_titles[event["id"]]

        if title in existing_worksheets:
            worksheet = existing_worksheets[title]
            worksheet.clear()
        else:
            worksheet = spreadsheet.add_worksheet(title=title, rows=200, cols=9)

        event_guests = [guest for guest in data["guests"] if guest["eventId"] == event["id"]]

        rows = [
            ["Guest", "Phone", "Email", "Friends", "DJ / Team Member", "Text", "User", "Checked In", "Not Checked In"],
        ]

        for guest in event_guests:
            guest_name = f'{guest.get("firstName", "")} {guest.get("lastName", "")}'.strip()
            is_checked_in = bool(guest.get("checkedIn"))
            rows.append([
                guest_name,
                guest.get("phone", ""),
                guest.get("email", ""),
                guest.get("friends", "") if int(guest.get("friends") or 0) >= 1 else "",
                guest.get("teamMember", "") or guest.get("dj", ""),
                guest.get("text", "Pending"),
                guest.get("lastEditedBy", "") or guest.get("createdBy", ""),
                1 if is_checked_in else 0,
                0 if is_checked_in else 1
            ])

        worksheet.update(rows)

    return {"enabled": True, "message": "Google Sheets synced successfully."}


def save_and_sync(data):
    save_data(data)
    return sync_google_sheets()


@app.before_request
def require_login_for_app():
    public_endpoints = {
        "home",
        "login",
        "logout",
        "request_pin_setup",
        "setup_pin",
        "set_pin",
        "login_with_pin",
        "privacy_policy",
        "terms_and_conditions",
        "static"
    }

    if request.endpoint in public_endpoints or request.path.startswith("/static/"):
        return None

    if not session.get("member_id"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Please log in first."}), 401
        return redirect(url_for("login"))

    return None


@app.route("/")
def home():
    if session.get("member_id"):
        return redirect(url_for("guest_manager"))
    return redirect(url_for("login"))


@app.route("/app")
def guest_manager():
    return render_template("index.html", current_member=get_current_member())




@app.route("/privacy-policy")
def privacy_policy():
    return render_template("privacy_policy.html")


@app.route("/terms-and-conditions")
def terms_and_conditions():
    return render_template("terms_and_conditions.html")

@app.route("/login")
def login():
    if session.get("member_id"):
        return redirect(url_for("guest_manager"))
    return render_template(
        "login.html",
        team_members=load_team_members(),
        setup_success=request.args.get("setup") == "success"
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/auth/request-pin-setup", methods=["POST"])
def request_pin_setup():
    payload = request.get_json(force=True)
    name = payload.get("name", "").strip()
    email = payload.get("email", "").strip().lower()

    if name not in load_team_members():
        return jsonify({"error": "Select a valid team member."}), 400

    if not is_valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400

    with get_auth_connection() as connection:
        member = connection.execute("SELECT * FROM team_members WHERE name = ?", (name,)).fetchone()
        if not member:
            return jsonify({"error": "Team member not found."}), 404

        approved_email = (member["email"] or "").strip().lower()
        if not is_valid_email(approved_email):
            return jsonify({
                "error": "No approved email is configured for this team member. Add their email to team_members.txt as: Name,email@example.com."
            }), 403

        if approved_email != email:
            return jsonify({"error": "That email does not match the approved email for this team member."}), 403

        now = datetime.now(timezone.utc)
        token = secrets.token_urlsafe(32)
        expires_at = (now + timedelta(hours=PIN_TOKEN_HOURS)).isoformat()
        connection.execute(
            "UPDATE team_members SET updated_at = ? WHERE id = ?",
            (now.isoformat(), member["id"])
        )
        connection.execute(
            "INSERT INTO pin_setup_tokens (member_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (member["id"], hash_token(token), expires_at, now.isoformat())
        )

    setup_url = url_for("setup_pin", token=token, _external=True)
    email_result = send_pin_setup_email(email, name, setup_url)

    return jsonify({
        "success": True,
        "message": "Verification started. Check your email for the one-time PIN setup link.",
        "email": email_result
    })


@app.route("/setup-pin/<token>")
def setup_pin(token):
    with get_auth_connection() as connection:
        rows = connection.execute(
            """
            SELECT pin_setup_tokens.*, team_members.name
            FROM pin_setup_tokens
            JOIN team_members ON team_members.id = pin_setup_tokens.member_id
            WHERE used_at IS NULL
            """
        ).fetchall()

    now = datetime.now(timezone.utc)
    for row in rows:
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at > now and token_matches(row["token_hash"], token):
            return render_template("set_pin.html", token=token, member_name=row["name"], expired=False)

    return render_template("set_pin.html", token="", member_name="", expired=True), 400


@app.route("/api/auth/set-pin", methods=["POST"])
def set_pin():
    payload = request.get_json(force=True)
    token = payload.get("token", "")
    pin = payload.get("pin", "")
    pin_confirm = payload.get("pinConfirm", "")

    if not re.fullmatch(r"\d{4,8}", pin):
        return jsonify({"error": "PIN must be 4 to 8 digits."}), 400

    if pin != pin_confirm:
        return jsonify({"error": "PIN confirmation does not match."}), 400

    with get_auth_connection() as connection:
        rows = connection.execute(
            """
            SELECT pin_setup_tokens.*, team_members.name
            FROM pin_setup_tokens
            JOIN team_members ON team_members.id = pin_setup_tokens.member_id
            WHERE used_at IS NULL
            """
        ).fetchall()

        now = datetime.now(timezone.utc)
        matched_row = None
        for row in rows:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at > now and token_matches(row["token_hash"], token):
                matched_row = row
                break

        if not matched_row:
            return jsonify({"error": "This setup link is invalid, expired, or already used."}), 400

        connection.execute(
            "UPDATE team_members SET pin_hash = ?, updated_at = ? WHERE id = ?",
            (generate_password_hash(pin), now.isoformat(), matched_row["member_id"])
        )
        connection.execute(
            "UPDATE pin_setup_tokens SET used_at = ? WHERE id = ?",
            (now.isoformat(), matched_row["id"])
        )

    return jsonify({"success": True, "redirectUrl": url_for("login", setup="success")})


@app.route("/api/auth/login", methods=["POST"])
def login_with_pin():
    payload = request.get_json(force=True)
    name = payload.get("name", "").strip()
    pin = payload.get("pin", "")

    member = get_member_by_name(name)
    if not member or not member["pin_hash"] or not check_password_hash(member["pin_hash"], pin):
        return jsonify({"error": "Invalid team member or PIN."}), 401

    session.clear()
    session["member_id"] = member["id"]
    session["member_name"] = member["name"]

    return jsonify({"success": True, "redirectUrl": url_for("guest_manager")})



@app.route("/api/sync-status", methods=["GET"])
def sync_status():
    result = sync_google_sheets()
    return jsonify(result)


@app.route("/api/events", methods=["GET"])
def get_events():
    data = load_data()
    return jsonify(data["events"])


@app.route("/api/team-members", methods=["GET"])
def get_team_members():
    return jsonify(load_team_members())


@app.route("/api/events", methods=["POST"])
def create_event():
    payload = request.get_json(force=True)

    event_name = payload.get("name", "").strip()
    djs_raw = payload.get("djs", "")

    if isinstance(djs_raw, str):
        djs = [dj.strip() for dj in djs_raw.split(",") if dj.strip()]
    else:
        djs = [str(dj).strip() for dj in djs_raw if str(dj).strip()]

    if not event_name:
        return jsonify({"error": "Event name is required."}), 400

    if not djs:
        return jsonify({"error": "At least one DJ is required."}), 400

    data = load_data()

    new_event = {
        "id": f"event_{uuid.uuid4().hex[:8]}",
        "name": event_name,
        "djs": djs,
        "teamMembers": []
    }

    data["events"].append(new_event)
    sync_result = save_and_sync(data)

    return jsonify({"event": new_event, "sync": sync_result}), 201


@app.route("/api/events/<event_id>/djs", methods=["POST"])
def add_dj_to_event(event_id):
    payload = request.get_json(force=True)
    dj_name = payload.get("dj", "").strip()

    if not dj_name:
        return jsonify({"error": "DJ name is required."}), 400

    data = load_data()
    event = next((event for event in data["events"] if event["id"] == event_id), None)

    if not event:
        return jsonify({"error": "Event not found."}), 404

    event.setdefault("djs", [])

    if any(existing_dj.strip().lower() == dj_name.lower() for existing_dj in event["djs"]):
        return jsonify({"error": "That DJ is already listed for this event."}), 400

    event["djs"].append(dj_name)
    sync_result = save_and_sync(data)

    return jsonify({"event": event, "sync": sync_result})


@app.route("/api/events/<event_id>", methods=["DELETE"])
def delete_event(event_id):
    data = load_data()

    original_event_count = len(data["events"])
    event_to_delete = next((event for event in data["events"] if event["id"] == event_id), None)

    data["events"] = [event for event in data["events"] if event["id"] != event_id]
    data["guests"] = [guest for guest in data["guests"] if guest["eventId"] != event_id]

    if len(data["events"]) == original_event_count:
        return jsonify({"error": "Event not found."}), 404

    sync_result = save_and_sync(data)

    return jsonify({
        "success": True,
        "removedEventId": event_id,
        "removedEventName": event_to_delete["name"] if event_to_delete else "",
        "sync": sync_result
    })


@app.route("/api/guests", methods=["GET"])
def get_guests():
    data = load_data()
    return jsonify(data["guests"])



def resolve_event_by_import_value(data, event_value):
    cleaned = (event_value or "").strip()
    if not cleaned:
        return None

    for event in data["events"]:
        if event.get("id") == cleaned or event.get("name", "").strip().lower() == cleaned.lower():
            return event

    return None


def resolve_import_role(event, role_value):
    cleaned = (role_value or "").strip()
    if not cleaned:
        return "", "", "DJ/Team Member is required."

    event_djs = event.get("djs", [])
    team_members = load_team_members()

    matching_dj = next((dj for dj in event_djs if dj.strip().lower() == cleaned.lower()), "")
    matching_team_member = next((member for member in team_members if member.strip().lower() == cleaned.lower()), "")

    if matching_dj:
        return matching_dj, "", ""

    if matching_team_member:
        return "", matching_team_member, ""

    return "", "", "DJ/Team Member must match a DJ on the selected event or a name in team_members.txt."


def build_guest_from_fields(data, fields, force_add=False):
    first_name = (fields.get("firstName") or "").strip()
    last_name = (fields.get("lastName") or "").strip()
    phone = (fields.get("phone") or "").strip()
    email = (fields.get("email") or "").strip()
    event_id = (fields.get("eventId") or "").strip()
    selected_dj = (fields.get("dj") or "").strip()
    selected_team_member = (fields.get("teamMember") or "").strip()

    try:
        friends = parse_friends_count(fields.get("friends", ""))
    except ValueError as error:
        return None, str(error), None

    if friends < 0:
        return None, "Friends cannot be negative.", None

    if not first_name or not last_name:
        return None, "First and last name are required.", None

    contact_errors = contact_validation_errors(phone, email)
    if contact_errors and not force_add:
        return None, " ".join(contact_errors), None

    if not event_id:
        return None, "Event selection is required.", None

    if not selected_dj and not selected_team_member:
        return None, "Select either a DJ or a team member.", None

    if selected_dj and selected_team_member:
        return None, "Select only one: either a DJ or a team member, not both.", None

    if selected_team_member and selected_team_member not in load_team_members():
        return None, "Selected team member is not listed in team_members.txt.", None

    matching_event = next((event for event in data["events"] if event["id"] == event_id), None)

    if not matching_event:
        return None, "Selected event does not exist.", None

    if selected_dj and selected_dj not in matching_event["djs"]:
        return None, "Selected DJ is not listed for this event.", None

    new_guest = {
        "id": f"guest_{uuid.uuid4().hex[:8]}",
        "firstName": first_name,
        "lastName": last_name,
        "phone": phone,
        "email": email,
        "friends": friends,
        "eventId": event_id,
        "eventName": matching_event["name"],
        "dj": selected_dj,
        "teamMember": selected_team_member,
        "text": "Pending",
        "smsConfirmationSent": False,
        "smsConfirmationSentAt": "",
        "smsConfirmationStatus": "",
        "checkedIn": False,
        "createdBy": get_current_member_name(),
        "lastEditedBy": get_current_member_name()
    }

    return new_guest, "", matching_event

@app.route("/api/guests", methods=["POST"])
def create_guest():
    payload = request.get_json(force=True)
    data = load_data()

    new_guest, error_message, _matching_event = build_guest_from_fields(
        data,
        {
            "firstName": payload.get("firstName", ""),
            "lastName": payload.get("lastName", ""),
            "phone": payload.get("phone", ""),
            "email": payload.get("email", ""),
            "friends": payload.get("friends", ""),
            "eventId": payload.get("eventId", ""),
            "dj": payload.get("dj", ""),
            "teamMember": payload.get("teamMember", ""),
        },
        force_add=bool(payload.get("forceAdd"))
    )

    if error_message:
        return jsonify({"error": error_message}), 400

    sms_result = send_guest_confirmation_sms(new_guest)
    data["guests"].append(new_guest)
    sync_result = save_and_sync(data)

    return jsonify({"guest": new_guest, "sync": sync_result, "sms": sms_result}), 201


@app.route("/api/guests/import", methods=["POST"])
def import_guests():
    upload = request.files.get("file")

    if not upload or not upload.filename:
        return jsonify({"error": "Choose a CSV file to import."}), 400

    if not upload.filename.lower().endswith(".csv"):
        return jsonify({"error": "Imported file must be a .csv file."}), 400

    try:
        raw_text = upload.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "CSV must be saved as UTF-8 text."}), 400

    reader = csv.reader(io.StringIO(raw_text))
    rows = list(reader)

    if not rows:
        return jsonify({"error": "CSV file is empty."}), 400

    expected_columns = ["first name", "last name", "phone number", "email", "friends", "event", "dj/team member"]
    first_row = [cell.strip().lower() for cell in rows[0]]
    has_header = first_row == expected_columns
    data_rows = rows[1:] if has_header else rows

    data = load_data()
    imported_guests = []
    skipped_rows = []
    sms_results = []

    for row_number, row in enumerate(data_rows, start=2 if has_header else 1):
        if not any(str(cell).strip() for cell in row):
            continue

        if len(row) != 7:
            skipped_rows.append({
                "row": row_number,
                "error": "Row must have exactly 7 columns: first name,last name,phone number,email,friends,event,DJ/Team Member"
            })
            continue

        first_name, last_name, phone, email, friends, event_value, role_value = [cell.strip() for cell in row]
        matching_event = resolve_event_by_import_value(data, event_value)

        if not matching_event:
            skipped_rows.append({"row": row_number, "error": f"Event not found: {event_value}"})
            continue

        selected_dj, selected_team_member, role_error = resolve_import_role(matching_event, role_value)

        if role_error:
            skipped_rows.append({"row": row_number, "error": f"{role_error} Value: {role_value}"})
            continue

        new_guest, error_message, _matching_event = build_guest_from_fields(
            data,
            {
                "firstName": first_name,
                "lastName": last_name,
                "phone": phone,
                "email": email,
                "friends": friends,
                "eventId": matching_event["id"],
                "dj": selected_dj,
                "teamMember": selected_team_member,
            },
            force_add=False
        )

        if error_message:
            skipped_rows.append({"row": row_number, "error": error_message})
            continue

        sms_result = send_guest_confirmation_sms(new_guest)
        data["guests"].append(new_guest)
        imported_guests.append(new_guest)
        sms_results.append({"guestId": new_guest["id"], "sent": sms_result.get("sent"), "message": sms_result.get("message", "")})

    if not imported_guests and skipped_rows:
        return jsonify({"error": "No guests were imported.", "skippedRows": skipped_rows}), 400

    sync_result = save_and_sync(data) if imported_guests else sync_google_sheets()

    return jsonify({
        "imported": len(imported_guests),
        "skipped": len(skipped_rows),
        "guests": imported_guests,
        "skippedRows": skipped_rows,
        "sms": sms_results,
        "sync": sync_result,
    }), 201


@app.route("/api/guests/<guest_id>/check-in", methods=["PATCH"])
def check_in_guest(guest_id):
    data = load_data()

    guest = next((guest for guest in data["guests"] if guest["id"] == guest_id), None)

    if not guest:
        return jsonify({"error": "Guest not found."}), 404

    guest["checkedIn"] = True
    guest["lastEditedBy"] = get_current_member_name()
    sync_result = save_and_sync(data)

    return jsonify({"guest": guest, "sync": sync_result})


@app.route("/api/guests/<guest_id>/uncheck", methods=["PATCH"])
def uncheck_guest(guest_id):
    data = load_data()

    guest = next((guest for guest in data["guests"] if guest["id"] == guest_id), None)

    if not guest:
        return jsonify({"error": "Guest not found."}), 404

    guest["checkedIn"] = False
    guest["lastEditedBy"] = get_current_member_name()
    sync_result = save_and_sync(data)

    return jsonify({"guest": guest, "sync": sync_result})


@app.route("/api/guests/<guest_id>", methods=["DELETE"])
def remove_guest(guest_id):
    data = load_data()

    original_guest_count = len(data["guests"])
    data["guests"] = [guest for guest in data["guests"] if guest["id"] != guest_id]

    if len(data["guests"]) == original_guest_count:
        return jsonify({"error": "Guest not found."}), 404

    sync_result = save_and_sync(data)

    return jsonify({"success": True, "removedGuestId": guest_id, "sync": sync_result})



@app.route("/api/sms/inbound", methods=["POST"])
def receive_sms_reply():
    """
    Twilio webhook endpoint.
    Configure your Twilio number's incoming message webhook to:
      https://YOUR-DOMAIN.com/api/sms/inbound
    """
    payload = request.get_json(silent=True) or {}
    inbound_phone = request.form.get("From", "") or payload.get("From", "")
    body = request.form.get("Body", "") or payload.get("Body", "")
    reply = (body or "").strip().upper()

    data = load_data()
    guest = find_pending_guest_by_phone(data, inbound_phone)

    if not guest:
        return Response("<Response></Response>", mimetype="application/xml")

    if reply == "Y":
        guest["text"] = "Y"
        guest["checkedIn"] = True
        send_sms_message(guest.get("phone", ""), SMS_Y_REPLY_MESSAGE)
    elif reply == "N":
        guest["text"] = "N"
        send_sms_message(guest.get("phone", ""), SMS_N_REPLY_MESSAGE)
    else:
        send_sms_message(guest.get("phone", ""), "Please reply Y to confirm attendance or N to cancel.")
        return Response("<Response></Response>", mimetype="application/xml")

    save_and_sync(data)
    return Response("<Response></Response>", mimetype="application/xml")


init_auth_db()


if __name__ == "__main__":
    app.run(debug=True)
