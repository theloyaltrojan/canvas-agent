"""
Canvas New Assignment Watcher
Polls Canvas for assignments, compares against saved state, and sends
a Telegram notification when new ones are posted.

State is stored in state/seen_assignments.json and committed back to
the repo after each run so the next run knows what's already been seen.
"""

import os
import json
import sys
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ── Config ────────────────────────────────────────────────────────────────────
CANVAS_TOKEN     = os.environ["CANVAS_TOKEN"]
CANVAS_DOMAIN    = os.environ["CANVAS_DOMAIN"].removeprefix("https://").removeprefix("http://").rstrip("/")
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DAYS_AHEAD       = int(os.environ.get("DAYS_AHEAD", "60"))

# Leave WATCHED_CLASSES empty to watch everything (except ignored courses).
# Set to comma-separated substrings to watch only those classes,
# e.g. "Algebra,Biology,Spanish"
WATCHED_CLASSES = [s.strip().lower() for s in os.environ.get("WATCHED_CLASSES", "").split(",") if s.strip()]
IGNORED_COURSES = [s.strip().lower() for s in os.environ.get("IGNORED_COURSES", "PE,Stagecraft").split(",") if s.strip()]

STATE_FILE = Path("state/seen_assignments.json")
# ─────────────────────────────────────────────────────────────────────────────


def canvas_get(path: str, params: dict = None) -> list:
    url     = f"https://{CANVAS_DOMAIN}/api/v1{path}"
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
    results = []
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return data
        url, params = None, None
        for part in resp.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
    return results


def is_watched(course_name: str) -> bool:
    name    = course_name.lower()
    no_spc  = name.replace(" ", "")
    # Always skip ignored courses
    if any(ig in name or ig in no_spc for ig in IGNORED_COURSES):
        return False
    # If no watch list, monitor everything not ignored
    if not WATCHED_CLASSES:
        return True
    return any(w in name or w in no_spc for w in WATCHED_CLASSES)


def load_state() -> dict | None:
    """Returns None on first run (no state file yet)."""
    if not STATE_FILE.exists():
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(seen_ids: set) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({
            "seen_ids":   sorted(seen_ids),
            "last_check": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)


def fetch_assignments() -> dict[int, dict]:
    """Returns {assignment_id: assignment_info} for all watched courses."""
    courses = canvas_get("/courses", {
        "enrollment_state": "active",
        "state[]":          ["available"],
        "per_page":         50,
    })

    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=DAYS_AHEAD)
    result = {}

    for course in courses:
        if not (isinstance(course, dict) and "id" in course and "name" in course):
            continue
        if not is_watched(course["name"]):
            continue

        try:
            asgns = canvas_get(f"/courses/{course['id']}/assignments", {"per_page": 50})
        except requests.HTTPError as e:
            print(f"  ⚠️  Skipping {course['name']}: {e}", file=sys.stderr)
            continue

        for a in asgns:
            if a.get("due_at"):
                due = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00"))
                if due > cutoff:
                    continue  # too far out
            result[a["id"]] = {
                "id":     a["id"],
                "name":   a["name"],
                "course": course["name"],
                "due_at": a.get("due_at"),
                "points": a.get("points_possible"),
                "url":    a.get("html_url", ""),
            }

    return result


def fmt_assignment(a: dict) -> str:
    pts = f"  •  {int(a['points'])} pts" if a.get("points") else ""
    if a.get("due_at"):
        due     = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00"))
        due_str = due.strftime("%-I:%M %p, %b %-d")
    else:
        due_str = "No due date"
    return (
        f"📝 <b>{a['name']}</b>{pts}\n"
        f"   📖 {a['course']}\n"
        f"   ⏰ Due: {due_str}\n"
        f"   🔗 <a href='{a['url']}'>Open in Canvas</a>"
    )


def send_telegram(text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    resp.raise_for_status()


def main():
    print("Loading saved state…")
    state = load_state()

    print("Fetching current Canvas assignments…")
    current     = fetch_assignments()
    current_ids = set(current.keys())
    print(f"  Found {len(current_ids)} assignment(s) across watched courses.")

    if state is None:
        # First run — save current state without sending any alerts
        save_state(current_ids)
        print(f"✅ First run. Saved {len(current_ids)} known assignments. "
              "Future new assignments will trigger notifications.")
        return

    seen_ids = set(state.get("seen_ids", []))
    new_ids  = current_ids - seen_ids

    if not new_ids:
        print("No new assignments found.")
        save_state(current_ids)
        return

    new_asgns = sorted(
        [current[i] for i in new_ids],
        key=lambda a: a.get("due_at") or "9999",
    )
    print(f"🔔 {len(new_asgns)} new assignment(s) found — sending notification…")

    n      = len(new_asgns)
    header = f"📣 <b>{'New Assignment Posted!' if n == 1 else f'{n} New Assignments Posted!'}</b>\n\n"
    body   = "\n\n".join(fmt_assignment(a) for a in new_asgns)
    send_telegram(header + body)

    save_state(current_ids)
    print("✅ Notification sent and state updated.")


if __name__ == "__main__":
    main()
