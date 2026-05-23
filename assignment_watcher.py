"""
Canvas Assignment & Grade Watcher
Detects two things and sends Telegram notifications for each:
  1. Newly posted assignments
  2. New or changed grades in the gradebook

Both respect the WATCHED_CLASSES filter set via the Telegram bot.
State is stored in state/seen_assignments.json and committed back to
the repo after every run.
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
    name   = course_name.lower()
    no_spc = name.replace(" ", "")
    if any(ig in name or ig in no_spc for ig in IGNORED_COURSES):
        return False
    if not WATCHED_CLASSES:
        return True
    return any(w in name or w in no_spc for w in WATCHED_CLASSES)


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None     # first run
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(seen_ids: set, grades: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({
            "seen_assignment_ids": sorted(seen_ids),
            "grades":              grades,
            "last_check":         datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)


# ── Canvas data fetching ──────────────────────────────────────────────────────

def get_watched_courses() -> list[dict]:
    courses = canvas_get("/courses", {
        "enrollment_state": "active",
        "state[]":          ["available"],
        "per_page":         50,
    })
    return [c for c in courses
            if isinstance(c, dict) and "id" in c and "name" in c
            and is_watched(c["name"])]


def fetch_assignments(courses: list[dict]) -> dict[int, dict]:
    """Returns {assignment_id: info} for upcoming assignments in watched courses."""
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=DAYS_AHEAD)
    result = {}

    for course in courses:
        try:
            asgns = canvas_get(f"/courses/{course['id']}/assignments", {"per_page": 50})
        except requests.HTTPError as e:
            print(f"  ⚠️  Skipping assignments for {course['name']}: {e}", file=sys.stderr)
            continue

        for a in asgns:
            if a.get("due_at"):
                due = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00"))
                if due > cutoff:
                    continue
            result[a["id"]] = {
                "id":     a["id"],
                "name":   a["name"],
                "course": course["name"],
                "due_at": a.get("due_at"),
                "points": a.get("points_possible"),
                "url":    a.get("html_url", ""),
            }

    return result


def fetch_grades(courses: list[dict]) -> dict[str, dict]:
    """
    Returns {str(assignment_id): grade_info} for all graded submissions.
    Keyed by string so JSON round-trips cleanly.
    """
    result = {}

    for course in courses:
        try:
            subs = canvas_get(f"/courses/{course['id']}/submissions", {
                "include[]": ["assignment"],
                "per_page":  100,
            })
        except requests.HTTPError as e:
            print(f"  ⚠️  Skipping grades for {course['name']}: {e}", file=sys.stderr)
            continue

        for s in subs:
            aid = s.get("assignment_id")
            if not aid:
                continue
            asgn = s.get("assignment") or {}
            result[str(aid)] = {
                "score":          s.get("score"),           # None = not yet graded
                "assignment_name": asgn.get("name", "Unknown assignment"),
                "points_possible": asgn.get("points_possible"),
                "course":         course["name"],
                "url":            asgn.get("html_url", ""),
            }

    return result


# ── Notification formatting ───────────────────────────────────────────────────

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


def fmt_grade_change(info: dict, old_score, new_score) -> str:
    pts  = info.get("points_possible")
    pts_str = f"/{int(pts)}" if pts else ""

    if old_score is None:
        # Brand new grade
        pct = f"  ({new_score / pts * 100:.1f}%)" if pts and pts > 0 else ""
        score_str = f"<b>{new_score}{pts_str}{pct}</b>"
        label = f"✏️ {score_str}"
    else:
        # Grade changed
        delta     = new_score - old_score
        sign      = "+" if delta >= 0 else ""
        pct       = f"  ({new_score / pts * 100:.1f}%)" if pts and pts > 0 else ""
        score_str = f"<b>{old_score} → {new_score}{pts_str}{pct}  ({sign}{delta:g} pts)</b>"
        label     = f"✏️ {score_str}"

    return (
        f"{label}\n"
        f"   📝 {info['assignment_name']}\n"
        f"   📖 {info['course']}\n"
        f"   🔗 <a href='{info['url']}'>Open in Canvas</a>"
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading saved state…")
    state = load_state()

    print("Fetching watched courses…")
    courses = get_watched_courses()
    print(f"  {len(courses)} watched course(s).")

    print("Fetching assignments…")
    current_assignments = fetch_assignments(courses)

    print("Fetching grades…")
    current_grades = fetch_grades(courses)

    # ── First run: initialise without notifying ───────────────────────────────
    if state is None:
        save_state(set(current_assignments.keys()), current_grades)
        print(f"✅ First run. Saved {len(current_assignments)} assignments "
              f"and {len(current_grades)} grade entries. "
              "Future changes will trigger notifications.")
        return

    # ── Detect new assignments ────────────────────────────────────────────────
    seen_ids  = set(state.get("seen_assignment_ids", []))
    new_ids   = set(current_assignments.keys()) - seen_ids
    new_asgns = sorted(
        [current_assignments[i] for i in new_ids],
        key=lambda a: a.get("due_at") or "9999",
    )

    if new_asgns:
        n      = len(new_asgns)
        header = f"📣 <b>{'New Assignment Posted!' if n == 1 else f'{n} New Assignments Posted!'}</b>\n\n"
        send_telegram(header + "\n\n".join(fmt_assignment(a) for a in new_asgns))
        print(f"🔔 Sent notification for {n} new assignment(s).")
    else:
        print("  No new assignments.")

    # ── Detect grade changes ──────────────────────────────────────────────────
    old_grades    = state.get("grades", {})
    grade_changes = []

    for aid, info in current_grades.items():
        new_score = info.get("score")
        if new_score is None:
            continue                             # still ungraded — skip

        old_entry = old_grades.get(aid, {})
        old_score = old_entry.get("score")

        if old_score == new_score:
            continue                             # no change

        grade_changes.append((info, old_score, new_score))

    if grade_changes:
        n      = len(grade_changes)
        header = f"📊 <b>{'Grade Posted!' if n == 1 else f'{n} Grades Posted/Updated!'}</b>\n\n"
        body   = "\n\n".join(fmt_grade_change(info, old, new) for info, old, new in grade_changes)
        send_telegram(header + body)
        print(f"🔔 Sent notification for {n} grade change(s).")
    else:
        print("  No grade changes.")

    # ── Save updated state ────────────────────────────────────────────────────
    save_state(set(current_assignments.keys()), current_grades)
    print("✅ State saved.")


if __name__ == "__main__":
    main()
