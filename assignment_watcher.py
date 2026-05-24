"""
Canvas Assignment & Grade Watcher
Detects four things and sends separate Telegram notifications for each:
  1. Newly posted assignments
  2. New or changed grades in the gradebook
  3. Assignments due within DUE_SOON_HOURS (default 2h) that aren't submitted
  4. Missing assignments (past due, nothing submitted)

All checks respect the WATCHED_CLASSES filter set via the Telegram bot.
State is stored in state/seen_assignments.json and committed back each run.
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
DUE_SOON_HOURS   = int(os.environ.get("DUE_SOON_HOURS", "2"))

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
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(seen_ids: set, grades: dict,
               alerted_due_soon: set, missing_notified: set) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({
            "seen_assignment_ids": sorted(seen_ids),
            "alerted_due_soon":    sorted(alerted_due_soon),
            "missing_notified":    sorted(missing_notified),
            "grades":              grades,
            "last_check":          datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)


# ── Canvas data ───────────────────────────────────────────────────────────────

def get_watched_courses() -> list[dict]:
    courses = canvas_get("/courses", {
        "enrollment_state": "active",
        "state[]":          ["available"],
        "per_page":         50,
    })
    return [c for c in courses
            if isinstance(c, dict) and "id" in c and "name" in c
            and is_watched(c["name"])]


def fetch_course_data(courses: list[dict]) -> tuple[dict, dict]:
    """
    Single pass per course — fetches both assignments and submissions.

    Returns:
        assignments : {int(id): assignment_info}
        submissions : {str(id): submission_info}   (string keys survive JSON)
    """
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=DAYS_AHEAD)
    assignments = {}
    submissions = {}

    for course in courses:
        cname = course["name"]

        # ── Assignments ───────────────────────────────────────────────────────
        try:
            for a in canvas_get(f"/courses/{course['id']}/assignments", {"per_page": 50}):
                if a.get("due_at"):
                    due = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00"))
                    if due > cutoff:
                        continue
                assignments[a["id"]] = {
                    "id":     a["id"],
                    "name":   a["name"],
                    "course": cname,
                    "due_at": a.get("due_at"),
                    "points": a.get("points_possible"),
                    "url":    a.get("html_url", ""),
                }
        except requests.HTTPError as e:
            print(f"  ⚠️  Assignments – {cname}: {e}", file=sys.stderr)

        # ── Submissions ───────────────────────────────────────────────────────
        try:
            for s in canvas_get(f"/courses/{course['id']}/submissions", {
                "include[]": ["assignment"],
                "per_page":  100,
            }):
                aid  = s.get("assignment_id")
                if not aid:
                    continue
                asgn = s.get("assignment") or {}
                submissions[str(aid)] = {
                    "score":           s.get("score"),
                    "submitted":       bool(
                        s.get("submitted_at") or
                        s.get("workflow_state") in ("submitted", "graded")
                    ),
                    "missing":         bool(s.get("missing")),
                    "excused":         bool(s.get("excused")),
                    "assignment_name": asgn.get("name", "Unknown"),
                    "points_possible": asgn.get("points_possible"),
                    "course":          cname,
                    "url":             asgn.get("html_url", ""),
                }
        except requests.HTTPError as e:
            print(f"  ⚠️  Submissions – {cname}: {e}", file=sys.stderr)

    return assignments, submissions


# ── Formatters ────────────────────────────────────────────────────────────────

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
    pts     = info.get("points_possible")
    pts_str = f"/{int(pts)}" if pts else ""
    pct     = f"  ({new_score / pts * 100:.1f}%)" if pts and pts > 0 else ""

    if old_score is None:
        label = f"✏️ <b>{new_score}{pts_str}{pct}</b>"
    else:
        delta = new_score - old_score
        sign  = "+" if delta >= 0 else ""
        label = f"✏️ <b>{old_score} → {new_score}{pts_str}{pct}  ({sign}{delta:g} pts)</b>"

    return (
        f"{label}\n"
        f"   📝 {info['assignment_name']}\n"
        f"   📖 {info['course']}\n"
        f"   🔗 <a href='{info['url']}'>Open in Canvas</a>"
    )


def fmt_due_soon(a: dict) -> str:
    due     = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00"))
    due_str = due.strftime("%-I:%M %p")
    pts     = f"  •  {int(a['points'])} pts" if a.get("points") else ""
    return (
        f"📝 <b>{a['name']}</b>{pts}\n"
        f"   📖 {a['course']}\n"
        f"   ⏰ Due at {due_str}\n"
        f"   🔗 <a href='{a['url']}'>Open in Canvas</a>"
    )


def fmt_missing(sub: dict) -> str:
    pts = f"  •  {int(sub['points_possible'])} pts" if sub.get("points_possible") else ""
    return (
        f"📝 <b>{sub['assignment_name']}</b>{pts}\n"
        f"   📖 {sub['course']}\n"
        f"   🔗 <a href='{sub['url']}'>Open in Canvas</a>"
    )


def send_telegram(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    ).raise_for_status()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading state…")
    state = load_state()

    print("Fetching watched courses…")
    courses = get_watched_courses()
    print(f"  {len(courses)} course(s).")

    print("Fetching assignments & submissions…")
    assignments, submissions = fetch_course_data(courses)
    print(f"  {len(assignments)} assignment(s), {len(submissions)} submission(s).")

    now = datetime.now(timezone.utc)

    # ── First run — save baseline, no alerts ─────────────────────────────────
    if state is None:
        save_state(set(assignments.keys()), submissions, set(), set())
        print(f"✅ First run. Baseline saved. Future changes will trigger notifications.")
        return

    seen_ids         = set(state.get("seen_assignment_ids", []))
    alerted_due_soon = set(state.get("alerted_due_soon", []))
    missing_notified = set(state.get("missing_notified", []))
    old_grades       = state.get("grades", {})

    # ── 1. New assignments ────────────────────────────────────────────────────
    new_asgns = sorted(
        [assignments[i] for i in set(assignments.keys()) - seen_ids],
        key=lambda a: a.get("due_at") or "9999",
    )
    if new_asgns:
        n = len(new_asgns)
        send_telegram(
            f"📣 <b>{'New Assignment Posted!' if n == 1 else f'{n} New Assignments Posted!'}</b>\n\n"
            + "\n\n".join(fmt_assignment(a) for a in new_asgns)
        )
        print(f"🔔 {n} new assignment(s).")
    else:
        print("  No new assignments.")

    # ── 2. Grade changes ──────────────────────────────────────────────────────
    grade_changes = []
    for aid, info in submissions.items():
        new_score = info.get("score")
        if new_score is None:
            continue
        old_score = old_grades.get(aid, {}).get("score")
        if old_score != new_score:
            grade_changes.append((info, old_score, new_score))

    if grade_changes:
        n = len(grade_changes)
        send_telegram(
            f"📊 <b>{'Grade Posted!' if n == 1 else f'{n} Grades Posted/Updated!'}</b>\n\n"
            + "\n\n".join(fmt_grade_change(i, o, n_) for i, o, n_ in grade_changes)
        )
        print(f"🔔 {n} grade change(s).")
    else:
        print("  No grade changes.")

    # ── 3. Due-soon alerts ────────────────────────────────────────────────────
    soon_window = now + timedelta(hours=DUE_SOON_HOURS)
    due_soon = []
    for aid, a in assignments.items():
        if not a.get("due_at"):
            continue
        due = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00"))
        if not (now <= due <= soon_window):
            continue
        sub = submissions.get(str(aid), {})
        if sub.get("submitted") or sub.get("excused"):
            continue
        if aid in alerted_due_soon:
            continue
        due_soon.append(a)
        alerted_due_soon.add(aid)

    # Clean up IDs for assignments that are now past due (keep state tidy)
    alerted_due_soon = {
        aid for aid in alerted_due_soon
        if aid in assignments and assignments[aid].get("due_at") and
        datetime.fromisoformat(assignments[aid]["due_at"].replace("Z", "+00:00")) > now
    }

    if due_soon:
        n = len(due_soon)
        send_telegram(
            f"⏰ <b>Due in {DUE_SOON_HOURS} Hour{'s' if DUE_SOON_HOURS != 1 else ''}!</b>\n\n"
            + "\n\n".join(fmt_due_soon(a) for a in due_soon)
        )
        print(f"🔔 {n} due-soon alert(s).")
    else:
        print("  No due-soon alerts.")

    # ── 4. Missing assignments ────────────────────────────────────────────────
    newly_missing = []
    for aid_str, sub in submissions.items():
        if not sub.get("missing") or sub.get("excused"):
            continue
        aid_int = int(aid_str)
        if aid_int in missing_notified:
            continue
        newly_missing.append(sub)
        missing_notified.add(aid_int)

    # Remove from missing_notified if the assignment has since been submitted
    missing_notified = {
        aid for aid in missing_notified
        if not submissions.get(str(aid), {}).get("submitted")
    }

    if newly_missing:
        n = len(newly_missing)
        send_telegram(
            f"🚨 <b>{'Missing Assignment!' if n == 1 else f'{n} Missing Assignments!'}</b>\n"
            f"<i>Past due with no submission</i>\n\n"
            + "\n\n".join(fmt_missing(s) for s in newly_missing)
        )
        print(f"🔔 {n} missing assignment(s).")
    else:
        print("  No newly missing assignments.")

    # ── Save state ────────────────────────────────────────────────────────────
    save_state(set(assignments.keys()), submissions, alerted_due_soon, missing_notified)
    print("✅ State saved.")


if __name__ == "__main__":
    main()
