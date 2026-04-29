"""Integration test for calendar-agent and proactive reminder injection."""

import asyncio
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = "http://localhost:8080"


def http_post(path, payload):
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return None, str(exc)


def http_get(path):
    url = f"{BASE_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except Exception as exc:
        return None, str(exc)


async def test_calendar_agent_list():
    print("=" * 60)
    print("TEST 1: Calendar-Agent List Events Request")
    print("=" * 60)

    payload = {
        "text": "Was steht morgen im Kalender?",
        "language": "de",
    }

    status, data = http_post("/api/conversation", payload)
    if status is None:
        print(f"Error: {data}")
        return None

    print(f"Status: {status}")
    speech = data.get("speech", "")
    routed_to = data.get("routed_to", "")
    print(f"Routed to: {routed_to}")
    print(f"Speech: {speech[:200]}")

    if "calendar-agent" in routed_to:
        print("[OK] Calendar-Agent was invoked!")
    else:
        print(f"[INFO] Routed to: {routed_to}")
    return data


async def test_calendar_agent_create():
    print()
    print("=" * 60)
    print("TEST 2: Calendar-Agent Create Event Request")
    print("=" * 60)

    now = datetime.now(timezone.utc)
    start = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    end = (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "text": f"Erstelle einen Test-Termin am {start} bis {end}",
        "language": "de",
    }

    status, data = http_post("/api/conversation", payload)
    if status is None:
        print(f"Error: {data}")
        return None

    print(f"Status: {status}")
    speech = data.get("speech", "")
    routed_to = data.get("routed_to", "")
    print(f"Routed to: {routed_to}")
    print(f"Speech: {speech[:200]}")

    if "calendar-agent" in routed_to:
        print("[OK] Calendar-Agent was invoked!")
    else:
        print(f"[INFO] Routed to: {routed_to}")
    return data


async def test_reminder_injection():
    print()
    print("=" * 60)
    print("TEST 3: Proactive Reminder Injection")
    print("=" * 60)
    print("Sending normal 'light on' request...")
    print()

    payload = {
        "text": "Schalte das Licht im Wohnzimmer an",
        "language": "de",
    }

    status, data = http_post("/api/conversation", payload)
    if status is None:
        print(f"Error: {data}")
        return None

    print(f"Status: {status}")
    speech = data.get("speech", "")
    routed_to = data.get("routed_to", "")
    print(f"Routed to: {routed_to}")
    print(f"Speech: {speech[:300]}")

    reminder_keywords = ["uebrigens", "termin", "morgen", "stunde", "minute"]
    if any(kw in speech.lower() for kw in reminder_keywords):
        print("[OK] Reminder was injected into response!")
    else:
        print("[INFO] No active reminder to inject (no upcoming events within reminder windows)")
    return data


async def test_dashboard_api():
    print()
    print("=" * 60)
    print("TEST 4: Calendar Admin API")
    print("=" * 60)

    status, data = http_get("/api/admin/calendar/calendars")
    print(f"GET /api/admin/calendar/calendars -> {status}")
    if status == 200:
        print(f"Calendars: {data[:200]}")
        print("[OK] Calendar admin API is accessible!")
    elif status == 401:
        print("[INFO] Requires admin authentication (expected without session)")
    else:
        print(f"[INFO] Status: {status}, Response: {data[:200]}")


async def main():
    print("Calendar Feature Integration Tests")
    print("=" * 60)
    print(f"Target: {BASE_URL}")
    print()

    # Check health
    status, data = http_get("/api/health")
    if status == 200:
        print(f"Health check: {status} OK")
    else:
        print(f"Health check failed: {status} {data}")
        print("Is the container running?")
        sys.exit(1)

    await test_calendar_agent_list()
    await test_calendar_agent_create()
    await test_reminder_injection()
    await test_dashboard_api()

    print()
    print("=" * 60)
    print("Tests completed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
