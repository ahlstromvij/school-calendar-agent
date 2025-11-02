# School calendar agent

## Why a school calendar agent?

Any parent or carer who has a child in school, or several children at different schools, knows that shools tend to send out lots of emails with information about activities, events, etc. It can be difficult to keep up with all of these emails on a daily basis.

That's why I build this agent. Every evening, it reads all school emails, pulls out any actions and dates from them, and puts these in a Google calendar. All you need to do is keep an eye on evenst that land in that calendar.

## What the code does

This repository contains code that

- authenticates against an email account
- reads emails form specific email addresses
- pulls out any relevant dates and actions from those emails
- creates calendar events for those actions

This workflow is triggered at 7pm every evening using GitHib Actions.

## Setup

The code assumes an `.env` file with the following:

```
GOOGLE_CLIENT_SECRET_PATH= ...
GOOGLE_TOKEN_PATH= ...
FAILED_EVENTS_LOG= ...
OPENAI_API_KEY= ...
GOOGLE_CALENDAR_ID= ...
SCHOOL_EMAILS= ...
```

`SCHOOL_EMAILS` should be comma-separated.

## Manual run

```
uv run python gmail_process.py
```

## Run Flask app

```
uv run python app.py
```