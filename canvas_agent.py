"""
Canvas Assignment Reminder Agent
Fetches upcoming assignments, calculates grade impact, prioritizes by
urgency + grade risk, and sends a daily Telegram digest.
"""

import os
import sys
import requests
from datetime import datetime, timezone, timedelta


# ── Config ────────────────────────────────────────────────────────────────────
CANVAS_TOKEN     = os.environ["CANVAS_TOKEN"]
CANVAS_DOMAIN    = os.environ["CANVAS_DOMAIN"].removeprefix("https://").removeprefix("http://").rstrip("/")
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DAYS_AHEAD       = int(os.environ.get("DAYS_AHEAD", "7"))

# Comma-separated substrings to ignore (case-insensitive)
_raw_ignored     = os.environ.get("IGNORED_COURSES", "PE,Stagecraft")
IGNORED_COURSES  = [s.strip().lower() for s in _raw_ignored.split(",") if s.strip()]
# ─────────────────────────────────────────────────────────────────────────────


# ── Canvas helpers ────────────────────────────────────────────────────────────

def canvas_get(path: str, params: dict = None) -> list:
    """Paginated GET against the Canvas REST API; returns all pages merged."""
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
            return data
        url, params = None, None
        for part in resp.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
    return results


def is_ignored(course_name: str) -> bool:
    # Collapse whitespace so "Stage Craft" matches "stagecraft"
    name = " ".join(course_name.lower().split())
    name_nospace = name.replace(" ", "")
    return any(ignored in name or ignored in name_nospace for ignored in IGNORED_COURSES)


# ── Grade math ────────────────────────────────────────────────────────────────

FALLBACK_GRADE_SCALE = [
    (93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"),
    (77, "C+"), (73, "C"), (70, "C-"), (67, "D+"), (63, "D"),
    (60, "D-"), (0,  "F"),
]

def letter_grade(pct: float | None, scheme: list = None) -> str:
    """
    Convert a percentage to a letter grade.
    Uses the course's Canvas grading scheme if provided, otherwise falls back
    to the standard scale.
    """
    if pct is None:
        return "N/A"
    if scheme:
        # Canvas scheme: list of {"name": "A", "value": 0.93} sorted descending
        for entry in sorted(scheme, key=lambda x: x["value"], reverse=True):
            if pct / 100 >= entry["value"]:
                return entry["name"]
        return scheme[-1]["name"]  # below every cutoff → lowest grade
    # Fallback
    for cutoff, letter in FALLBACK_GRADE_SCALE:
        if pct >= cutoff:
            return letter
    return "F"


def near_boundary(pct: float | None, scheme: list = None, within: float = 3.0) -> bool:
    """True if the grade is within `within` points of any letter-grade boundary."""
    if pct is None:
        return False
    if scheme:
        boundaries = [entry["value"] * 100 for entry in scheme]
    else:
        boundaries = [90, 87, 83, 80, 77, 73, 70, 67, 63, 60]
    return any(abs(pct - b) <= within for b in boundaries)


def new_grade_after(earned: float, possible: float,
                    assignment_pts: float, score_pct: float) -> float | None:
    """Grade (%) after scoring score_pct% on an assignment worth assignment_pts."""
    if possible <= 0 or assignment_pts <= 0:
        return None
    return ((earned + score_pct / 100 * assignment_pts) / (possible + assignment_pts)) * 100


# ── Priority scoring ──────────────────────────────────────────────────────────

def priority_score(due_at: datetime, current_grade: float | None,
                   max_impact: float) -> float:
    """
    Returns a 0-1 priority score.  Higher = more important to work on.
    Weights: urgency 40%, grade risk 30%, point impact 20%, boundary bonus 10%.
    """
    now = datetime.now(timezone.utc)
    days_left = max(0, (due_at - now).total_seconds() / 86400)

    urgency      = max(0.0, 1.0 - days_left / 7)
    grade_risk   = max(0.0, (85 - (current_grade or 85)) / 85)
    impact_score = min(max_impact / 10.0, 1.0) if max_impact else 0.0
    boundary     = 0.5 if near_boundary(current_grade) else 0.0

    return urgency * 0.40 + grade_risk * 0.30 + impact_score * 0.20 + boundary * 0.10


# ── Data fetching ─────────────────────────────────────────────────────────────

def get_points_from_submissions(course_id: int) -> tuple[float, float]:
    """
    Fallback when Canvas doesn't return current_points in enrollment data.
    Sums earned and possible points across all graded submissions.
    Returns (earned, possible).
    """
    try:
        subs = canvas_get(f"/courses/{course_id}/submissions", {
            "include[]": ["assignment"],
            "per_page":  100,
        })
        earned = possible = 0.0
        for s in subs:
            if s.get("score") is not None:
                earned   += float(s["score"])
                possible += float((s.get("assignment") or {}).get("points_possible") or 0)
        return earned, possible
    except Exception:
        return 0.0, 0.0


def get_active_courses() -> list[dict]:
    courses = canvas_get("/courses", {
        "enrollment_state": "active",
        "state[]":          ["available"],
        "include[]":        ["total_scores", "current_grading_period_scores", "grading_scheme"],
        "per_page":         50,
    })

    result = []
    for c in courses:
        if not (isinstance(c, dict) and "id" in c and "name" in c):
            continue
        if is_ignored(c["name"]):
            print(f"  ⏭  Ignoring: {c['name']}")
            continue

        current_score  = None
        current_earned = 0.0

        for enr in c.get("enrollments", []):
            if enr.get("type") == "student":
                current_score  = enr.get("computed_current_score")
                current_earned = enr.get("current_points") or 0.0
                break

        # Back-calculate total points possible from score % and points earned
        if current_score and current_score > 0 and current_earned:
            current_possible = (current_earned / current_score) * 100
        else:
            current_possible = 0.0

        # If Canvas didn't return point totals, fetch them from submissions directly
        if current_possible == 0 and current_score and current_score > 0:
            current_earned, current_possible = get_points_from_submissions(c["id"])

        # Canvas returns grading_scheme as either:
        #   [{"name": "A", "value": 0.93}, ...]  or  [["A", 0.93], ...]
        # Normalize to the dict form.
        raw_scheme = c.get("grading_scheme") or []
        scheme = []
        for entry in raw_scheme:
            if isinstance(entry, (list, tuple)):
                scheme.append({"name": entry[0], "value": entry[1]})
            else:
                scheme.append(entry)

        result.append({
            "id":               c["id"],
            "name":             c["name"],
            "current_score":    current_score,
            "current_earned":   current_earned,
            "current_possible": current_possible,
            "grading_scheme":   scheme,  # [] = use fallback scale
        })
    return result


def get_upcoming_assignments(course: dict) -> list[dict]:
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=DAYS_AHEAD)

    raw = canvas_get(f"/courses/{course['id']}/assignments", {
        "order_by": "due_at",
        "per_page": 50,
    })

    upcoming = []
    for a in raw:
        due_str = a.get("due_at")
        if not due_str:
            continue
        due = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
        if not (now <= due <= cutoff):
            continue

        pts = float(a.get("points_possible") or 0)
        ce  = course["current_earned"]
        cp  = course["current_possible"]
        cs  = course["current_score"]

        g100 = new_grade_after(ce, cp, pts, 100)
        g80  = new_grade_after(ce, cp, pts,  80)
        g60  = new_grade_after(ce, cp, pts,  60)
        g0   = new_grade_after(ce, cp, pts,   0)

        max_impact = (g100 - cs) if (g100 is not None and cs is not None) else 0.0
        prio = priority_score(due, cs, max_impact)

        upcoming.append({
            "name":           a["name"],
            "course":         course["name"],
            "course_grade":   cs,
            "grading_scheme": course["grading_scheme"],
            "due_at":         due,
            "points":         pts,
            "url":            a.get("html_url", ""),
            "submitted":      bool(a.get("has_submitted_submissions")),
            "g100":           g100,
            "g80":            g80,
            "g60":            g60,
            "g0":             g0,
            "max_impact":     max_impact,
            "priority":       prio,
        })
    return upcoming


# ── Message formatting ────────────────────────────────────────────────────────

def fmt_impact(current: float | None, new: float | None, scheme: list) -> str:
    if current is None or new is None:
        return "—"
    delta = new - current
    sign  = "+" if delta >= 0 else ""
    return f"{new:.1f}% ({letter_grade(new, scheme)})  {sign}{delta:.1f}%"


def build_message(assignments: list[dict]) -> str:
    now      = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %B %d")

    header = (
        f"📚 <b>Canvas Digest — {date_str}</b>\n"
        f"Next {DAYS_AHEAD} days · {len(assignments)} assignment(s)\n"
        f"Sorted by priority ↓\n"
    )

    if not assignments:
        return header + "\n✅ Nothing due — you're all caught up!"

    sorted_asgn = sorted(assignments, key=lambda x: x["priority"], reverse=True)
    lines = [header]

    for i, a in enumerate(sorted_asgn, 1):
        days = (a["due_at"].date() - now.date()).days

        if days == 0:
            when = "🔴 <b>DUE TODAY</b>"
        elif days == 1:
            when = "🟠 <b>DUE TOMORROW</b>"
        elif days <= 3:
            when = f"🟡 <b>IN {days} DAYS</b>"
        else:
            when = f"🟢 <b>IN {days} DAYS</b>"

        due_fmt  = a["due_at"].strftime("%-I:%M %p, %b %-d")
        pts_str  = f"{int(a['points'])} pts" if a["points"] else "ungraded"
        check    = " · ✅ submitted" if a["submitted"] else ""
        cg     = a["course_grade"]
        scheme = a["grading_scheme"]
        cg_str   = f"{cg:.1f}% ({letter_grade(cg, scheme)})" if cg is not None else "N/A"
        boundary = " ⚠️ <i>near grade boundary</i>" if near_boundary(cg, scheme) else ""

        block = (
            f"\n<b>#{i}  {a['name']}</b>{check}\n"
            f"     {when}  ·  ⏰ {due_fmt}  ·  📝 {pts_str}\n"
            f"     📖 {a['course']}\n"
            f"     Current grade: <b>{cg_str}</b>{boundary}\n"
        )

        if a["g100"] is not None and a["points"] > 0:
            block += (
                f"     <b>Grade impact:</b>\n"
                f"       💯 100% → {fmt_impact(cg, a['g100'], scheme)}\n"
                f"       👍  80% → {fmt_impact(cg, a['g80'], scheme)}\n"
                f"       📉  60% → {fmt_impact(cg, a['g60'], scheme)}\n"
                f"       ❌   0% → {fmt_impact(cg, a['g0'],  scheme)}\n"
            )

        block += f"     🔗 <a href='{a['url']}'>Open in Canvas</a>"
        lines.append(block)

    return "\n".join(lines)


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching Canvas courses…")
    courses = get_active_courses()
    print(f"  {len(courses)} active course(s) after filtering.")

    all_assignments = []
    for course in courses:
        try:
            asgns    = get_upcoming_assignments(course)
            grade_s  = f"{course['current_score']:.1f}%" if course["current_score"] is not None else "N/A"
            print(f"  {course['name']} [{grade_s}]: {len(asgns)} upcoming")
            all_assignments.extend(asgns)
        except requests.HTTPError as e:
            print(f"  ⚠️  Skipping {course['name']}: {e}", file=sys.stderr)

    message = build_message(all_assignments)
    print("\nSending Telegram message…")
    send_telegram(message)
    print("✅ Done!")


if __name__ == "__main__":
    main()
