from __future__ import print_function
import os
import json
import re
import base64
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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
PROCESSED_LABEL_NAME = "SCHOOL-PROCESSED"
LOCAL_TZ = os.getenv("LOCAL_TZ", "Europe/London")  # used for event times

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


def get_or_create_label(service, name):
    """Return labelId for `name`, creating it if missing."""
    user_id = "me"
    labels = service.users().labels().list(userId=user_id).execute().get("labels", [])
    for lab in labels:
        if lab["name"] == name:
            return lab["id"]
    created = service.users().labels().create(
        userId=user_id,
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def extract_plain_text_body(payload):
    parts = payload.get("parts", [])
    if parts:
        for p in parts:
            if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(p["body"]["data"]).decode()
    return base64.urlsafe_b64decode(payload.get("body", {}).get("data", b"")).decode()


def list_school_emails(service, days=60, include_only_inbox=True, max_results_per_page=500):
    """
    Return full Gmail messages for the target senders within the last `days`.
    Handles correct OR query and paginates through all result pages.
    """
    if not SCHOOL_EMAILS:
        return []

    # Build a single OR-based query so Gmail matches any of the senders.
    # Example: from:(a@x.com OR b@y.com) newer_than:60d label:inbox
    senders_or = " OR ".join(SCHOOL_EMAILS)
    q_parts = [f"from:({senders_or})", f"newer_than:{days}d"]
    if include_only_inbox:
        q_parts.append("label:inbox")
    # Exclude already processed messages
    q_parts.append(f"-label:{PROCESSED_LABEL_NAME}")
    q = " ".join(q_parts)

    user_id = "me"
    messages = []
    page_token = None

    while True:
        list_kwargs = {
            "userId": user_id,
            "q": q,
            "maxResults": max_results_per_page,
        }
        if page_token:
            list_kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**list_kwargs).execute()
        ids = resp.get("messages", [])
        if not ids:
            break

        # Fetch each message in 'full' so you have headers and payload
        for m in ids:
            msg = service.users().messages().get(userId=user_id, id=m["id"], format="full").execute()
            messages.append(msg)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return messages


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
    """
    Return (start_iso, end_iso, use_datetime).
    - Understands '1pm', '1:30pm', '1.30pm', '1–2pm', '1:15-2:45 pm', 'noon', 'midnight'.
    - Applies Europe/London (configurable via LOCAL_TZ) and includes offset in ISO string.
    - For all-day events, returns date-only strings.
    """
    if not event.get("date"):
        return None, None, False

    # Parse the date (strip ordinals like 1st/2nd)
    date_str = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", event["date"].strip(), flags=re.IGNORECASE)
    try:
        base_date = dateparser.parse(date_str, fuzzy=True).date()
    except Exception:
        print(f"⚠️ Could not parse date: {event['date']}")
        return None, None, False

    tz = ZoneInfo(LOCAL_TZ)
    tstr = (event.get("time") or "").strip()
    # Remove notes in parentheses
    tstr = re.sub(r"\(.*?\)", "", tstr, flags=re.DOTALL).strip()
    low = tstr.lower()

    # All-day hints
    if not low or any(x in low for x in ["all day", "tbc", "to be confirmed"]):
        start = base_date.isoformat()
        end = (base_date + timedelta(days=1)).isoformat()
        return start, end, False

    # Normalize common words
    low = low.replace("midday", "noon")
    # Build helpers
    def token_to_hm(tok: str, default_meridiem: str | None = None):
        tok = tok.strip().lower()
        tok = tok.replace("a.m.", "am").replace("p.m.", "pm").replace("a.m", "am").replace("p.m", "pm")
        tok = tok.replace(" ", "")
        tok = tok.replace("–", "-").replace("—", "-")
        tok = tok.replace(".", ":")  # 1.30pm -> 1:30pm
        if tok in ("noon",):
            return 12, 0, "pm"
        if tok in ("midnight",):
            return 0, 0, "am"
        m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", tok)
        if not m:
            # 24h like 13:00 or 0900
            m24 = re.match(r"^(\d{1,2}):?(\d{2})$", tok)
            if m24:
                h = int(m24.group(1)); mi = int(m24.group(2))
                return h, mi, None
            return None
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        mer = (m.group(3) or default_meridiem)
        return h, mi, mer

    # Extract possible range "t1 - t2"
    range_re = re.compile(
        r"(?i)\b(from\s+)?(?P<t1>(?:noon|midnight|\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?))\s*(?:to|-|–|—)\s*(?P<t2>(?:noon|midnight|\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?))\b"
    )
    single_re = re.compile(r"(?i)\b(noon|midnight|\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\b")

    start_hm = end_hm = None
    m = range_re.search(tstr)
    if m:
        t1 = m.group("t1")
        t2 = m.group("t2")
        # If only second has am/pm, propagate to first
        mer2 = re.search(r"(?i)am|pm|a\.m\.|p\.m\.", t2)
        default_mer = None
        if mer2:
            default_mer = "am" if "a" in mer2.group(0).lower() else "pm"
        start_hm = token_to_hm(t1, default_mer)
        end_hm = token_to_hm(t2, default_mer)
    else:
        # Look for any single time token
        m2 = single_re.search(tstr)
        if m2:
            start_hm = token_to_hm(m2.group(1))

    # If still nothing usable, default to 17:00-18:00
    if not start_hm:
        start_hm = (17, 0, None)
    if not end_hm and start_hm:
        # default 1 hour duration
        sh, sm, smer = start_hm
        end_hm = (sh, sm, smer)
        # compute end by adding 1 hour after conversion below

    def to_24h(h, mi, mer):
        if mer:
            mer = mer.lower()
            if h == 12 and mer == "am":
                h = 0
            elif h != 12 and mer == "pm":
                h += 12
        # no meridiem => assume 24h if h >= 0
        return h, mi

    sh, sm, smer = start_hm
    eh, em, emer = end_hm
    # Propagate meridiem end->start if start missing
    if smer is None and emer is not None:
        smer = emer
    sh, sm = to_24h(sh, sm, smer)
    eh, em = to_24h(eh, em, emer)

    start_dt = datetime.combine(base_date, datetime.min.time()).replace(tzinfo=tz).replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_dt = datetime.combine(base_date, datetime.min.time()).replace(tzinfo=tz).replace(hour=eh, minute=em, second=0, microsecond=0)
    # If end <= start, assume it was a range like "1–2pm" parsed ok; if equal (single token), add 1 hour
    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=1)

    return start_dt.isoformat(), end_dt.isoformat(), True


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
    # Ensure the processed label exists and capture its ID
    processed_label_id = get_or_create_label(gmail_service, PROCESSED_LABEL_NAME)
    
    emails = list_school_emails(gmail_service)
    print(f"📩 Found {len(emails)} school emails\n")

    summary = {"added": 0, "skipped": 0, "no_event": 0, "parse_error": 0, "error": 0}

    for idx, email in enumerate(emails, 1):
        # Decode the email body to plain text for extraction
        email_text = extract_plain_text_body(email.get("payload", {})) or email.get("snippet", "")
        event = extract_event_from_email(email_text)

        if isinstance(event, dict):
            print(f"📬 Email {idx}: Event extracted → {event['event_name']}")
            result = add_event_to_calendar(calendar_service, event)
            if result == "skipped":
                summary["skipped"] += 1
            elif result:
                summary["added"] += 1
            else:
                summary["error"] += 1
            # Mark email as processed
            try:
                gmail_service.users().messages().modify(
                    userId="me",
                    id=email["id"],
                    body={"addLabelIds": [processed_label_id], "removeLabelIds": []},
                ).execute()
            except Exception as e:
                print(f"⚠️ Could not label message as processed: {e}")
        elif event == "no_event":
            print(f"ℹ️ Email {idx}: No event found.")
            summary["no_event"] += 1
            # Also mark as processed so we don't re-check it every run
            try:
                gmail_service.users().messages().modify(
                    userId="me",
                    id=email["id"],
                    body={"addLabelIds": [processed_label_id], "removeLabelIds": []},
                ).execute()
            except Exception as e:
                print(f"⚠️ Could not label message as processed: {e}")
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
