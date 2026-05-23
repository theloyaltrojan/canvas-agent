/**
 * Cloudflare Worker — Canvas Reminder Telegram Bot
 *
 * Commands:
 *   /remind          — triggers the GitHub Actions daily digest
 *   /grades          — shows current grades instantly
 *   /due             — assignments due today & tomorrow
 *   /due <day>       — assignments due on a specific day
 *                      e.g. /due monday  /due friday  /due tomorrow
 *   /help            — lists commands
 *
 * Required environment variables (Cloudflare dashboard → Settings → Variables):
 *   TELEGRAM_TOKEN    — bot token from @BotFather
 *   TELEGRAM_CHAT_ID  — your numeric chat ID
 *   GITHUB_TOKEN      — PAT with `workflow` scope
 *   GITHUB_OWNER      — your GitHub username
 *   GITHUB_REPO       — repository name (canvas-agent)
 *   CANVAS_TOKEN      — Canvas API token
 *   CANVAS_DOMAIN     — e.g. sequoia.instructure.com  (no https://)
 *   IGNORED_COURSES   — comma-separated substrings to skip (e.g. "PE,Stagecraft")
 */

const DAYS = ["sunday","monday","tuesday","wednesday","thursday","friday","saturday"];
const SHORT_DAYS = ["sun","mon","tue","wed","thu","fri","sat"];

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("OK");

    let body;
    try { body = await request.json(); } catch { return new Response("OK"); }

    const message = body?.message;
    if (!message) return new Response("OK");

    const text   = (message?.text || "").trim();
    const chatId = String(message?.chat?.id);

    if (chatId !== env.TELEGRAM_CHAT_ID) return new Response("OK");

    const cmd = text.split(" ")[0].toLowerCase();

    if (cmd === "/remind") {
      const ok = await triggerWorkflow(env);
      await sendMessage(env, chatId,
        ok
          ? "⏳ On it! Your assignment digest will arrive in ~30 seconds."
          : "❌ Couldn't trigger the reminder — check that your GITHUB_TOKEN is still valid."
      );

    } else if (cmd === "/grades") {
      await sendMessage(env, chatId, "⏳ Fetching your grades...");
      await sendMessage(env, chatId, await buildGradesMessage(env));

    } else if (cmd === "/due") {
      const arg = text.slice(4).trim().toLowerCase();
      const target = parseTarget(arg);
      if (!target) {
        await sendMessage(env, chatId,
          "❓ Didn't recognize that day.\n\nTry:\n/due\n/due tomorrow\n/due monday\n/due friday"
        );
      } else {
        await sendMessage(env, chatId, `⏳ Checking assignments for ${target.label}...`);
        await sendMessage(env, chatId, await buildDueMessage(env, target));
      }

    } else if (cmd === "/start" || cmd === "/help") {
      await sendMessage(env, chatId,
        "📚 <b>Canvas Reminder Bot</b>\n\n" +
        "Commands:\n" +
        "/remind        — full assignment digest now\n" +
        "/grades        — all current grades\n" +
        "/due           — due today & tomorrow\n" +
        "/due tomorrow  — due tomorrow\n" +
        "/due monday    — due on Monday (any day works)\n" +
        "/help          — this message"
      );
    }

    return new Response("OK");
  },
};

// ── /due logic ────────────────────────────────────────────────────────────────

/**
 * Parse the argument after /due into a { label, dates[] } object.
 * dates is an array of Date objects (midnight local) to match against.
 */
function parseTarget(arg) {
  // Work in UTC-based "day" math using the date portion only
  const todayUTC = new Date();
  todayUTC.setUTCHours(0, 0, 0, 0);

  if (!arg || arg === "today") {
    const tomorrow = new Date(todayUTC);
    tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
    return { label: "Today & Tomorrow", dates: [todayUTC, tomorrow] };
  }

  if (arg === "tomorrow") {
    const tomorrow = new Date(todayUTC);
    tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
    return { label: "Tomorrow", dates: [tomorrow] };
  }

  let dayIndex = DAYS.indexOf(arg);
  if (dayIndex === -1) dayIndex = SHORT_DAYS.indexOf(arg);
  if (dayIndex === -1) return null;

  const currentDay = todayUTC.getUTCDay();
  let daysUntil = dayIndex - currentDay;
  if (daysUntil <= 0) daysUntil += 7;          // always next occurrence
  if (daysUntil === 7) daysUntil = 0;          // today matches → show today

  const target = new Date(todayUTC);
  target.setUTCDate(todayUTC.getUTCDate() + daysUntil);

  const label = DAYS[dayIndex].charAt(0).toUpperCase() + DAYS[dayIndex].slice(1);
  return { label, dates: [target] };
}

async function buildDueMessage(env, target) {
  const domain  = env.CANVAS_DOMAIN.replace(/^https?:\/\//, "").replace(/\/$/, "");
  const ignored = parseIgnored(env);

  let courses;
  try {
    courses = await canvasGet(env, `/courses?enrollment_state=active&state[]=available&per_page=50`);
  } catch (e) {
    return `❌ Couldn't fetch courses: ${e.message}`;
  }

  const active = courses
    .filter(c => c.id && c.name)
    .filter(c => !isIgnored(c.name, ignored));

  // Fetch assignments for all courses in parallel
  const targetDateStrings = new Set(target.dates.map(d => d.toISOString().slice(0, 10)));
  const lookAheadMs = 14 * 24 * 60 * 60 * 1000;
  const cutoff = new Date(Date.now() + lookAheadMs).toISOString();

  const assignmentLists = await Promise.all(
    active.map(course =>
      canvasGet(env, `/courses/${course.id}/assignments?order_by=due_at&per_page=50`)
        .then(list =>
          list
            .filter(a => a.due_at)
            .filter(a => {
              const dateStr = a.due_at.slice(0, 10); // "YYYY-MM-DD"
              return targetDateStrings.has(dateStr);
            })
            .map(a => ({
              name:      a.name,
              course:    course.name,
              due_at:    new Date(a.due_at),
              points:    a.points_possible,
              url:       a.html_url || "",
              submitted: !!a.has_submitted_submissions,
            }))
        )
        .catch(() => [])
    )
  );

  const assignments = assignmentLists.flat().sort((a, b) => a.due_at - b.due_at);

  if (assignments.length === 0) {
    return `✅ Nothing due ${target.label === "Today & Tomorrow" ? "today or tomorrow" : `on ${target.label}`} — enjoy the free time!`;
  }

  const lines = [`📅 <b>Due ${target.label}</b> — ${assignments.length} assignment(s)\n`];

  // Group by date when showing multiple days
  const byDate = {};
  for (const a of assignments) {
    const key = a.due_at.toISOString().slice(0, 10);
    if (!byDate[key]) byDate[key] = [];
    byDate[key].push(a);
  }

  for (const [dateKey, items] of Object.entries(byDate)) {
    if (Object.keys(byDate).length > 1) {
      const d = new Date(dateKey + "T12:00:00Z");
      lines.push(`\n<b>${d.toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric", timeZone: "UTC" })}</b>`);
    }
    for (const a of items) {
      const time      = a.due_at.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/Los_Angeles" });
      const pts       = a.points ? `  •  ${Math.round(a.points)} pts` : "";
      const check     = a.submitted ? "✅ " : "";
      lines.push(
        `${check}<b>${a.name}</b>${pts}\n` +
        `     📖 ${a.course}  ·  ⏰ ${time}\n` +
        `     🔗 <a href="${a.url}">Open in Canvas</a>`
      );
    }
  }

  return lines.join("\n");
}

// ── /grades logic ─────────────────────────────────────────────────────────────

async function buildGradesMessage(env) {
  let courses;
  try {
    courses = await canvasGet(env, `/courses?enrollment_state=active&state[]=available&include[]=total_scores&per_page=50`);
  } catch (e) {
    return `❌ Couldn't fetch grades: ${e.message}`;
  }

  const ignored = parseIgnored(env);
  const graded  = courses
    .filter(c => c.id && c.name && !isIgnored(c.name, ignored))
    .map(c => {
      const enr = (c.enrollments || []).find(e => e.type === "student");
      return { name: c.name, score: enr?.computed_current_score ?? null, grade: enr?.computed_current_grade ?? null };
    })
    .filter(c => c.score !== null)
    .sort((a, b) => a.score - b.score);

  if (graded.length === 0) return "📊 No graded courses found right now.";

  const lines = ["📊 <b>Current Grades</b>  (lowest first)\n"];
  for (const c of graded) {
    const emoji    = c.score >= 90 ? "🟢" : c.score >= 80 ? "🟡" : c.score >= 70 ? "🟠" : "🔴";
    const gradeStr = c.grade ? ` (${c.grade})` : "";
    lines.push(`${emoji} <b>${c.score.toFixed(1)}%${gradeStr}</b> — ${c.name}`);
  }
  return lines.join("\n");
}

// ── GitHub Actions ────────────────────────────────────────────────────────────

async function triggerWorkflow(env) {
  const resp = await fetch(
    `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}` +
    `/actions/workflows/daily_reminder.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization:  `Bearer ${env.GITHUB_TOKEN}`,
        Accept:         "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent":   "canvas-telegram-bot",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );
  return resp.ok;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

async function canvasGet(env, path) {
  const domain = env.CANVAS_DOMAIN.replace(/^https?:\/\//, "").replace(/\/$/, "");
  const url    = `https://${domain}/api/v1${path}`;
  const resp   = await fetch(url, {
    headers: { Authorization: `Bearer ${env.CANVAS_TOKEN}` },
  });
  if (!resp.ok) throw new Error(`Canvas API error ${resp.status} on ${path}`);
  return resp.json();
}

function parseIgnored(env) {
  return (env.IGNORED_COURSES || "PE,Stagecraft")
    .split(",")
    .map(s => s.trim().toLowerCase().replace(/\s+/g, ""));
}

function isIgnored(name, ignored) {
  const n  = name.toLowerCase();
  const ns = n.replace(/\s+/g, "");
  return ignored.some(ig => n.includes(ig) || ns.includes(ig));
}

async function sendMessage(env, chatId, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
}
