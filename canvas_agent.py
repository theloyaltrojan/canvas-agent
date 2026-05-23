"""
Canvas Assignment Reminder Agent
Fetches upcoming assignments from Canvas LMS and sends a daily
Telegram message summarizing what's due.
"""

import os
import sys
import requests
from datetime import datetime, timezone, timedelta


# ── Config ────────────────────────────────────────────────────────────────────
CANVAS_TOKEN    = os.environ["CANVAS_TOKEN"]
CANVAS_DOMAIN   = os.environ["CANVAS_DOMAIN"]   # e.g. "myschool.instructure.com"
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DAYS_AHEAD      = int(os.environ.get("DAYS_AHEAD", "7"))
# ─────────────────────────────────────────────────────────────────────────────


def canvas_get(path: str, params: dict = None) -> list:
    """Paginated GET against the Canvas REST API, returns all pages merged."""
    url = f"https://{CANVAS_DOMAIN}/api/v1{path}"
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
    results = []
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return data  # single-object endpoint
        # Follow Canvas pagination via Link header
        url = None
        params = None  # only send params on first request
        link_header = resp.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
    return results


def get_active_courses() -> list[dict]:
    courses = canvas_get("/courses", {
        "enrollment_state": "active",
        "state[]": ["available"],
        "per_page": 50,
    })
    return [c for c in courses if isinstance(c, dict) and "id" in c and "name" in c]


def get_upcoming_assignments(course_id: int, course_name: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=DAYS_AHEAD)

    assignments = canvas_get(f"/courses/{course_id}/assignments", {
        "order_by": "due_at",
        "per_page": 50,
    })

    upcoming = []
    for a in assignments:
        due_str = a.get("due_at")
        if not due_str:
            continue
        due = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
        if now <= due <= cutoff:
            upcoming.append({
                "name":    a["name"],
                "course":  course_name,
                "due_at":  due,
                "points":  a.get("points_possible"),
                "url":     a.get("html_url", ""),
                "submitted": bool(a.get("has_submitted_submissions")),
            })
    return upcoming


def build_message(assignments: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %B %d")

    header = (
        f"📚 <b>Daily Canvas Digest — {date_str}</b>\n"
        f"Next {DAYS_AHEAD} days · {len(assignments)} assignment(s)\n"
    )

    if not assignments:
        return header + "\n✅ Nothing due — enjoy the break!"

    # Bucket by urgency
    buckets = {"🔴 Due Today": [], "🟠 Due Tomorrow": [],
                "🟡 This Week": [], "🟢 Later": []}

    for a in sorted(assignments, key=lambda x: x["due_at"]):
        delta = (a["due_at"].date() - now.date()).days
        if delta == 0:
            buckets["🔴 Due Today"].append(a)
        elif delta == 1:
            buckets["🟠 Due Tomorrow"].append(a)
        elif delta <= 7:
            buckets["🟡 This Week"].append(a)
        else:
            buckets["🟢 Later"].append(a)

    lines = [header]
    for label, items in buckets.items():
        if not items:
            continue
        lines.append(f"\n<b>{label}</b>")
        for a in items:
            pts     = f"  •  {int(a['points'])} pts" if a.get("points") else ""
            due_fmt = a["due_at"].strftime("%-I:%M %p, %b %-d")
            check   = "✅ " if a["submitted"] else ""
            lines.append(
                f"{check}  <b>{a['name']}</b>{pts}\n"
                f"     📖 {a['course']}\n"
                f"     ⏰ {due_fmt}\n"
                f"     🔗 <a href='{a['url']}'>Open in Canvas</a>"
            )

    return "\n".join(lines)


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def main():
    print("Fetching Canvas courses…")
    courses = get_active_courses()
    print(f"  Found {len(courses)} active course(s).")

    all_assignments = []
    for course in courses:
        try:
            assignments = get_upcoming_assignments(course["id"], course["name"])
            print(f"  {course['name']}: {len(assignments)} upcoming")
            all_assignments.extend(assignments)
        except requests.HTTPError as e:
            print(f"  ⚠️  Skipping {course['name']}: {e}", file=sys.stderr)

    message = build_message(all_assignments)
    print("\nSending Telegram message…")
    send_telegram(message)
    print("✅ Done!")


if __name__ == "__main__":
    main()
