# Event Guest Tabs App

This version has:

1. Events
2. Create Event
3. Add Guest

There is no separate Guest List tab.

## Event Deleting

Inside the **Events** tab, hover over an event tile and a small **X** appears in the top-right corner.

Clicking that X deletes the event and also removes all guests attached to that event.

## Guest List Features

Inside each event dropdown, the guest list includes:

- Guest name
- Phone number
- Email
- Selected DJ
- Check-in status
- Check In button
- Uncheck button after check-in
- Remove Guest button

When a guest is checked in, that guest's row turns green. Clicking **Uncheck** turns the row back to normal. Clicking **Remove Guest** removes that guest from the event's guest list.

## Google Sheets Sync

The app can dynamically update a Google Sheet whenever:

- an event is created
- an event is deleted
- a guest is added
- a guest is removed
- a guest is checked in
- a guest is unchecked

The Google Sheet is sectioned by event. The app creates one worksheet/tab per event and an **Event Index** tab.

Each event worksheet includes:

- Event name
- DJs for that event
- First name
- Last name
- Phone
- Email
- Selected DJ
- Status
- Checked In

When guests are removed, their rows are removed from the Google Sheet. When events are deleted, that event's worksheet is deleted.

## Google Sheets Setup

### 1. Install requirements

```bash
python -m pip install -r requirements.txt
```

On Windows, this may be:

```bash
py -m pip install -r requirements.txt
```

### 2. Create a Google Sheet

Create a blank Google Sheet.

Copy the Sheet ID from the URL.

Example URL:

```text
https://docs.google.com/spreadsheets/d/THIS_PART_IS_THE_SHEET_ID/edit
```

### 3. Create Google service account credentials

In Google Cloud:

1. Create or open a Google Cloud project
2. Enable the Google Sheets API
3. Create a Service Account
4. Create a JSON key for that service account
5. Download the JSON file
6. Rename it to:

```text
service_account.json
```

7. Put `service_account.json` in the same folder as `app.py`

### 4. Share your Google Sheet

Open your downloaded `service_account.json`.

Find the service account email. It usually looks like:

```text
something@something.iam.gserviceaccount.com
```

Share your Google Sheet with that email as an editor.

### 5. Enable sync in the config file

Copy:

```text
google_sheets_config.example.json
```

Rename the copy to:

```text
google_sheets_config.json
```

Then edit it:

```json
{
  "enabled": true,
  "spreadsheet_id": "PASTE_YOUR_GOOGLE_SHEET_ID_HERE",
  "index_sheet_name": "Event Index",
  "protected_tabs": []
}
```

### 6. Run the app

```bash
python app.py
```

Or:

```bash
py app.py
```

Open:

```text
http://127.0.0.1:5000
```

The app will now sync Google Sheets after every event or guest change.

## Local Run Instructions Without Google Sheets

The app still works without Google Sheets. If you do not configure Google Sheets, it saves locally to `data.json`.

```bash
python -m pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

## Easier Google Sheets Setup Script

This package includes:

```text
setup_google_sheets.py
```

Run it from the same folder as `app.py`:

```bash
python setup_google_sheets.py
```

or on Windows:

```bash
py setup_google_sheets.py
```

The script asks for:

- Google Sheet ID
- Index sheet name
- Path to your downloaded service account JSON credentials file

Then it:

- copies your credentials file into the app folder as `service_account.json`
- creates `google_sheets_config.json`
- tests access to the Sheet
- runs the first sync
- creates/formats the Sheet tabs from your current app data

You still need to manually create the Google Sheet, create/download the Google service account JSON key, and share the Sheet with the service account email as an Editor.

## Clique Cabana Visual Theme

This version includes the Clique Cabana brand assets in the `static/` folder:

```text
static/clique-cabana-wordmark.png
static/clique-cabana-logo.png
static/clique-cabana-palm.png
```

The HTML has been restyled with a sleek black-and-white Clique Cabana theme, branded hero section, logo accents, softer glass-style cards, upgraded tabs, and event tiles that better match the provided visual references.

## Team Login + PIN Setup

This version now opens to a login page before the guest list dashboard.

Included login features:

- Team member dropdown pulled from `team_members.txt`
- PIN input for returning team members
- Create PIN flow for first-time users
- Email verification through a one-time setup link
- SQLite database storage in `auth.sqlite3`
- PINs are hashed with Werkzeug before being saved
- The original guest management dashboard is protected behind login
- API routes are protected too, so users cannot bypass the login page by calling guest/event endpoints directly

### Team member list

Edit this file to change who appears in the dropdown:

```text
team_members.txt
```

Basic format:

```text
Ryan Raitz
Abby Sparks
Ty Abbott
Bil Carter
```

Safer production format with approved emails:

```text
Ryan Raitz,ryan@example.com
Abby Sparks,abby@example.com
Ty Abbott,ty@example.com
Bil Carter,bil@example.com
```

If an approved email is listed, the user must type that exact email before receiving the one-time PIN setup link. This is the better way to verify that the person creating the PIN is the intended team member.

### Email setup

For real emails, set these environment variables before running the app:

```bash
SMTP_HOST=smtp.yourprovider.com
SMTP_PORT=587
SMTP_USER=your_smtp_username
SMTP_PASSWORD=your_smtp_password
FROM_EMAIL=no-reply@yourdomain.com
FLASK_SECRET_KEY=replace-this-with-a-long-random-secret
```

If SMTP is not configured, the app still works locally for testing. It prints the one-time setup link in the terminal and also shows a local development setup link on the page.

### Important security note

The app hashes PINs before saving them, so the stored database value is not readable as the original PIN. For the strongest real-world identity check, use the `Name,email@example.com` format in `team_members.txt` and only use email accounts that are already known to belong to the correct team members.

## Secure Login / PIN Setup

This version places a login page in front of the guest list manager.

Team members are loaded from:

```text
team_members.txt
```

Use this secure format:

```text
Ryan Raitz,ryan@example.com
Abby Sparks,abby@example.com
Ty Abbott,ty@example.com
Bil Carter,bil@example.com
```

The app now fails closed: a team member can only request a one-time PIN setup link when the email typed into the Create PIN form exactly matches the approved email stored beside that person's name in `team_members.txt`.

If a line only has a name, or the email is blank/invalid, that person still appears in the dropdown but cannot create a PIN until an approved email is added.

PINs are never stored in readable text. They are hashed with Werkzeug password hashing and saved in the local SQLite database:

```text
auth.sqlite3
```

One-time PIN setup links are also stored as hashed tokens, expire after 1 hour, and are marked used after a PIN is created.

### Email sending setup

To send real verification emails, set these environment variables before running the app:

```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@example.com
SMTP_PASSWORD=your-app-password
FROM_EMAIL=your-email@example.com
FLASK_SECRET_KEY=replace-this-with-a-long-random-secret
```

Without SMTP configured, the app prints the setup link in the terminal for local testing only.


## Latest visual update

This package uses the newest attached Clique Cabana assets for both the login/PIN flow and the main guest-management dashboard. The large teal/white wave wordmark is used as the main visual system, while the teal CC mark is used for icons, watermarks, cards, and background accents.

## SMS confirmation setup

This build includes optional Twilio SMS confirmation.

When a guest is added with a valid phone number, the app sends:

`REPLY Y to confirm attendance and N to cancel`

Guest rows now include a `Text` value:

- `Pending` = no Y/N reply yet
- `Y` = guest replied Y and is automatically checked in
- `N` = guest replied N and remains not checked in

To enable real SMS sending, install requirements and set these environment variables before running the app:

```bash
export TWILIO_ACCOUNT_SID="your_account_sid"
export TWILIO_AUTH_TOKEN="your_auth_token"
export TWILIO_FROM_NUMBER="+15555555555"
python app.py
```

Then configure your Twilio phone number's incoming message webhook to:

```text
https://YOUR-DOMAIN.com/api/sms/inbound
```

If Twilio is not configured, the app still runs normally and prints the SMS message to the terminal instead of sending it.

## CSV guest import

The Add Guest tab includes an **Import File** button for importing guests from a CSV file.

CSV columns must be in this exact order:

```csv
first name,last name,phone number,email,friends,event,DJ/Team Member
```

Notes:
- The first row may be the header above, or the file may start directly with guest rows.
- The `event` value must match an existing event name exactly, or the event ID.
- The `DJ/Team Member` value must match either a DJ already listed on that event or a name in `team_members.txt`.
- Imported guests are saved the same way manually added guests are saved.
- Each imported guest receives the same SMS confirmation request as a manually added guest when Twilio is configured. If Twilio is not configured, the text is printed in the terminal.

## Render restart/redeploy data recovery from Google Sheets

This version treats the linked Google Sheet as the recoverable source of truth when the app starts.

On startup, the app reads the configured Google Sheet, imports the Event Index tab, imports each event tab listed in the Sheet Tab column, rebuilds `data.json`, and then the Events page uses that rebuilt local data while the app is running.

Required Google Sheet structure:

Event Index columns:
`Event Name | DJs | Total Guests | Checked In | Not Checked In | Sheet Tab`

Each event tab columns:
`Guest | Phone | Email | Friends | DJ / Team Member | Text | User | Checked In | Not Checked In`

Important notes:
- Google Sheets must remain enabled in `google_sheets_config.json`.
- The Render service must have access to `service_account.json`, and the Google Sheet must be shared with the service account email.
- While the app is running, create/delete/check-in/uncheck actions still dynamically update Google Sheets.
- After a Render restart or redeploy, the app pulls the latest Sheet state back into the Events page.
- There is also a manual recovery endpoint: `POST /api/import-from-google-sheets`.
