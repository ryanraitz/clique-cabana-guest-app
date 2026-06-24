"""
Google Sheets Setup Helper

This script does the Google Sheets setup for the Event Guest Manager app.

What it does:
1. Asks for your Google Sheet ID
2. Asks for the desired index sheet name
3. Asks for the path to your downloaded Google service account JSON credentials file
4. Copies that credentials file into this app folder as service_account.json
5. Creates google_sheets_config.json
6. Tests whether the app can access your Google Sheet
7. Runs the first sync and formats/populates the Sheet using the current app data

Run from the same folder as app.py:

    python setup_google_sheets.py

or on Windows:

    py setup_google_sheets.py
"""

from pathlib import Path
import json
import shutil
import sys


APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "google_sheets_config.json"
SERVICE_ACCOUNT_FILE = APP_DIR / "service_account.json"
DATA_FILE = APP_DIR / "data.json"


def print_header():
    print()
    print("=" * 64)
    print("Google Sheets Setup Helper")
    print("=" * 64)
    print()
    print("Before continuing, make sure you have:")
    print("1. Created a blank Google Sheet")
    print("2. Copied the Google Sheet ID from the URL")
    print("3. Downloaded your Google service account JSON key")
    print("4. Shared the Google Sheet with the service account email as Editor")
    print()


def ask_required(prompt):
    while True:
        value = input(prompt).strip().strip('"').strip("'")
        if value:
            return value
        print("This field is required.")


def load_service_account_email(credentials_path):
    with credentials_path.open("r", encoding="utf-8") as file:
        credentials = json.load(file)

    email = credentials.get("client_email")

    if not email:
        raise ValueError(
            "Could not find client_email in the credentials file. "
            "Make sure this is a Google service account JSON key."
        )

    return email


def create_default_data_if_needed():
    if DATA_FILE.exists():
        return

    default_data = {
        "events": [
            {
                "id": "event_001",
                "name": "Opening Night",
                "djs": ["DJ Nova", "Harny", "Wave Runner"]
            },
            {
                "id": "event_002",
                "name": "After Hours Session",
                "djs": ["DJ Luna", "Midnight Mike"]
            }
        ],
        "guests": []
    }

    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(default_data, file, indent=2)

    print("Created starter data.json file.")


def install_hint_and_exit():
    print()
    print("Missing Google Sheets packages.")
    print("Run this command, then run this setup script again:")
    print()
    print("    python -m pip install -r requirements.txt")
    print()
    print("On Windows, you may need:")
    print()
    print("    py -m pip install -r requirements.txt")
    print()
    sys.exit(1)


def test_google_sheet_access(spreadsheet_id):
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        install_hint_and_exit()

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_file(str(SERVICE_ACCOUNT_FILE), scopes=scopes)
    client = gspread.authorize(credentials)

    spreadsheet = client.open_by_key(spreadsheet_id)
    return spreadsheet.title


def run_first_sync():
    try:
        import app as event_app
    except Exception as error:
        print()
        print("The setup files were created, but app.py could not be imported for the first sync.")
        print(f"Error: {error}")
        print()
        print("Try running the app normally:")
        print("    python app.py")
        return

    result = event_app.sync_google_sheets()
    print()
    print(result.get("message", "Sync completed."))


def main():
    print_header()

    spreadsheet_id = ask_required("Paste your Google Sheet ID: ")
    index_sheet_name = input("Index sheet name [Event Index]: ").strip() or "Event Index"
    credentials_path_raw = ask_required("Path to your downloaded service account JSON file: ")

    credentials_path = Path(credentials_path_raw).expanduser().resolve()

    if not credentials_path.exists():
        print()
        print(f"Could not find credentials file here:")
        print(credentials_path)
        sys.exit(1)

    try:
        service_account_email = load_service_account_email(credentials_path)
    except Exception as error:
        print()
        print(f"Credentials file problem: {error}")
        sys.exit(1)

    shutil.copy2(credentials_path, SERVICE_ACCOUNT_FILE)

    config = {
        "enabled": True,
        "spreadsheet_id": spreadsheet_id,
        "index_sheet_name": index_sheet_name,
        "protected_tabs": []
    }

    with CONFIG_FILE.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)

    create_default_data_if_needed()

    print()
    print("Setup files created:")
    print(f"- {CONFIG_FILE.name}")
    print(f"- {SERVICE_ACCOUNT_FILE.name}")
    print()
    print("Service account email:")
    print(service_account_email)
    print()
    print("Confirming Google Sheet access...")

    try:
        sheet_title = test_google_sheet_access(spreadsheet_id)
    except Exception as error:
        print()
        print("The config files were created, but the app could not access your Google Sheet.")
        print()
        print("Most common fix:")
        print(f"Share the Google Sheet with this service account email as Editor:")
        print(service_account_email)
        print()
        print(f"Error details: {error}")
        sys.exit(1)

    print(f"Connected successfully to Google Sheet: {sheet_title}")
    print()
    print("Running first sync...")
    run_first_sync()

    print()
    print("Done.")
    print()
    print("Now run the app:")
    print()
    print("    python app.py")
    print()
    print("or on Windows:")
    print()
    print("    py app.py")
    print()


if __name__ == "__main__":
    main()
