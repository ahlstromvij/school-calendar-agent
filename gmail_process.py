from __future__ import print_function
import os
import base64
import json
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.errors import HttpError
from openai import OpenAI
from dateutil import parser as dateparser
from difflib import SequenceMatcher


load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")  # use "primary" if not set
client = OpenAI()

FAILED_EVENTS_LOG = os.getenv("FAILED_EVENTS_LOG", "logs/failed_events.json")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
SCOPES = list(set(GMAIL_SCOPES + CALENDAR_SCOPES))  # unified auth

raw_school_emails = os.getenv("SCHOOL_EMAILS", "").strip()
SCHOOL_EMAILS = [email.strip() for email in raw_school_emails.split(",") if email.strip()]

def log_failed_event(event, error_msg):
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "event": event,
        "error": str(error_msg),
    }
    try:
        if os.path.exists(FAILED_EVENTS_LOG):
            with open(FAILED_EVENTS_LOG, "r", encoding="utf-8") as f:
                existing = json.load(f)
        else:
            existing = []
        existing.append(entry)
        with open(FAILED_EVENTS_LOG, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception as log_err:
        print(f"⚠️ Failed to log event error: {log_err}")


def get_google_service(api_name, api_version):
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
    client_secret_path = os.getenv("GOOGLE_CLIENT_SECRET_PATH", "client_secret.json")

    creds = None

    # Try to read token file robustly (handle dict or list)
    if os.path.exists(token_path):
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # If file contains a list, try to pick the first dict-looking entry
            if isinstance(data, list):
                picked = None
                for item in data:
                    if isinstance(item, dict) and ("token" in item or "refresh_token" in item or "access_token" in item):
                        picked = item
                        break
                if picked is None and len(data) == 1 and isinstance(data[0], dict):
                    picked = data[0]
                data = picked or {}
            if isinstance(data, dict) and data:
                creds = Credentials.from_authorized_user_info(data, SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_secret_path):
                raise FileNotFoundError(
                    f"Google client secret file not found at: {client_secret_path}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)

        dirpath = os.path.dirname(token_path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())

    return build(api_name, api_version, credentials=creds)


def extract_plain_text_body(payload):
    parts = payload.get("parts", [])
    if parts:
        for p in parts:
            if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(p["body"]["data"]).decode()
    return base64.urlsafe_b64decode(payload.get("body", {}).get("data", b"")).decode()


def list_school_emails(service):
    results = service.users().messages().list(userId="me", labelIds=["INBOX"]).execute()
    messages = results.get("messages", [])
    emails = []
    for msg in messages:
        msg_data = service.users().messages().get(userId="me", id=msg["id"]).execute()
        from_header = next((h["value"] for h in msg_data["payload"]["headers"] if h["name"] == "From"), "")
        if any(sender in from_header for sender in SCHOOL_EMAILS):
            body = extract_plain_text_body(msg_data["payload"])
            emails.append({"id": msg["id"], "body": body})
    return emails


def normalize_event(event_dict):
    if not event_dict:
        return None
    for key in ["event_name", "date", "time", "details", "reminders"]:
        if key not in event_dict or event_dict[key] is None:
            event_dict[key] = ""
        if isinstance(event_dict[key], list):
            event_dict[key] = " ".join(event_dict[key])
    return event_dict


def extract_event_from_email(email_text):
    if "please view this e-mail in an application that supports html" in email_text.lower():
        return "no_event"

    prompt = f"""
    Extract event info from this school email as JSON with keys:
    event_name, date, time, details, reminders
    Email:
    '''{email_text}'''
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        text_output = response.choices[0].message.content.strip()
        text_output = re.sub(r"^\s*json\s*", "", text_output, flags=re.IGNORECASE).strip("`").strip()
        match = re.search(r"\{.*\}", text_output, re.DOTALL)
        if not match:
            return "parse_error"

        json_text = match.group()
        try:
            event = json.loads(json_text)
        except json.JSONDecodeError:
            try:
                event = json.loads(json_text.replace("'", '"'))
            except json.JSONDecodeError:
                return "parse_error"

        event = normalize_event(event)
        if event and not any(event.values()):
            return "no_event"

        return event
    except Exception:
        return "error"


def parse_event_datetime(event):
    """Return start, end, and whether to use dateTime for Google Calendar."""
    if not event.get("date"):
        return None, None, False

    date_str = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", event["date"].strip())
    
    try:
        dt = dateparser.parse(date_str, fuzzy=True)
        start_date = dt.date().isoformat()
    except Exception:
        print(f"⚠️ Could not parse date: {event['date']}")
        return None, None, False

    use_datetime = False

    time_str = event.get("time", "").strip().lower()
    time_str = re.sub(r"\(.*?\)", "", time_str).strip()  # remove parenthetical notes

    if time_str:
        # Handle common vague times
        if "end of day" in time_str:
            start_time = "17:00"
            end_time = "18:00"
        elif "approx" in time_str or "around" in time_str:
            times = re.findall(r"(\d{1,2}:\d{2})", time_str)
            if times:
                start_time = times[0]
                end_time = (datetime.strptime(start_time, "%H:%M") + timedelta(hours=1)).strftime("%H:%M")
            else:
                start_time = "17:00"
                end_time = "18:00"
        else:
            # Extract start/end if given in HH:MM format
            times = re.findall(r"(\d{1,2}:\d{2})", time_str)
            if times:
                start_time = times[0]
                end_time = times[1] if len(times) > 1 else (datetime.strptime(start_time, "%H:%M") + timedelta(hours=1)).strftime("%H:%M")
            else:
                start_time = "17:00"
                end_time = "18:00"

        start = f"{start_date}T{start_time}:00Z"
        end = f"{start_date}T{end_time}:00Z"
        use_datetime = True
    else:
        # All-day event
        start = start_date
        end = (dt.date() + timedelta(days=1)).isoformat()
        use_datetime = False

    # Safety: ensure start != end
    if start == end:
        end = (dt + timedelta(hours=1)).isoformat() + "Z"
        use_datetime = True

    return start, end, use_datetime


def similar(a, b):
    """Return a similarity ratio between 0 and 1."""
    return SequenceMatcher(None, a, b).ratio()

def event_exists_in_calendar(service, event, similarity_threshold=0.85):
    """
    Check if a similar event exists in the calendar.
    similarity_threshold: 0-1, how closely names must match to be considered duplicate.
    """
    start, end, use_datetime = parse_event_datetime(event)
    if not start:
        return False

    time_min = f"{start}T00:00:00Z" if not use_datetime else start
    time_max = f"{end}T23:59:59Z" if not use_datetime else end

    try:
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
    except Exception as e:
        print(f"⚠️ Calendar query failed for {event['event_name']}: {e}")
        return False

    event_name = (event["event_name"] or "").strip().lower()
    for e in events_result.get("items", []):
        existing_name = (e.get("summary", "").strip().lower())
        if similar(event_name, existing_name) >= similarity_threshold:
            return True
    return False


def add_event_to_calendar(service, event):
    try:
        start, end, use_datetime = parse_event_datetime(event)
        if not start:
            print(f"⚠️ Skipping event '{event['event_name']}' — invalid date.")
            return None

        if event_exists_in_calendar(service, event):
            print(f"⏭️ Skipped duplicate: {event['event_name']}")
            return "skipped"

        event_body = {
            "summary": event.get("event_name", "School Event"),
            "description": f"{event.get('details', '')}\n\nReminders:\n{event.get('reminders', '')}",
            "start": {"dateTime": start} if use_datetime else {"date": start},
            "end": {"dateTime": end} if use_datetime else {"date": end},
        }

        created_event = service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
        print(f"✅ Added to Calendar: {created_event.get('htmlLink')}")
        return created_event.get("htmlLink")

    except HttpError as e:
        print(f"❌ Google API error: {e}")
        log_failed_event(event, e)
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        log_failed_event(event, e)
        return None


def main():
    if not os.path.exists(FAILED_EVENTS_LOG):
        with open(FAILED_EVENTS_LOG, "w", encoding="utf-8") as f:
            json.dump([], f)

    gmail_service = get_google_service("gmail", "v1")
    calendar_service = get_google_service("calendar", "v3")
    emails = list_school_emails(gmail_service)
    print(f"📩 Found {len(emails)} school emails\n")

    summary = {"added": 0, "skipped": 0, "no_event": 0, "parse_error": 0, "error": 0}

    for idx, email in enumerate(emails, 1):
        event = extract_event_from_email(email["body"])

        if isinstance(event, dict):
            print(f"📬 Email {idx}: Event extracted → {event['event_name']}")
            result = add_event_to_calendar(calendar_service, event)
            if result == "skipped":
                summary["skipped"] += 1
            elif result:
                summary["added"] += 1
            else:
                summary["error"] += 1
        elif event == "no_event":
            print(f"ℹ️ Email {idx}: No event found.")
            summary["no_event"] += 1
        elif event == "parse_error":
            print(f"⚠️ Email {idx}: JSON parsing failed.")
            summary["parse_error"] += 1
        else:
            print(f"❌ Email {idx}: Unexpected extraction error.")
            summary["error"] += 1

    print("\n📊 Summary:")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
