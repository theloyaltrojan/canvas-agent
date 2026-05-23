/**
 * Cloudflare Worker — Canvas Reminder Telegram Bot
 *
 * Commands:
 *   /remind  — triggers the GitHub Actions daily digest
 *   /grades  — fetches and displays current grades instantly
 *   /help    — lists commands
 *
 * Required environment variables (set in Cloudflare dashboard):
 *   TELEGRAM_TOKEN    — bot token from @BotFather
 *   TELEGRAM_CHAT_ID  — your numeric chat ID
 *   GITHUB_TOKEN      — PAT with `workflow` scope
 *   GITHUB_OWNER      — your GitHub username
 *   GITHUB_REPO       — repository name (canvas-agent)
 *   CANVAS_TOKEN      — Canvas API token
 *   CANVAS_DOMAIN     — e.g. sequoia.instructure.com  (no https://)
 *   IGNORED_COURSES   — comma-separated substrings to skip (e.g. "PE,Stagecraft")
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("OK");

    let body;
    try { body = await request.json(); } catch { return new Response("OK"); }

    const message = body?.message;
    if (!message) return new Response("OK");

    const text   = (message?.text || "").trim();
    const chatId = String(message?.chat?.id);

    // Security: ignore anyone who isn't you
    if (chatId !== env.TELEGRAM_CHAT_ID) return new Response("OK");

    if (text.startsWith("/remind")) {
      const ok = await triggerWorkflow(env);
      await sendMessage(env, chatId,
        ok
          ? "⏳ On it! Your assignment digest will arrive in ~30 seconds."
          : "❌ Couldn't trigger the reminder — check that your GITHUB_TOKEN is still valid."
      );

    } else if (text.startsWith("/grades")) {
      await sendMessage(env, chatId, "⏳ Fetching your grades...");
      const gradesMsg = await buildGradesMessage(env);
      await sendMessage(env, chatId, gradesMsg);

    } else if (text.startsWith("/start") || text.startsWith("/help")) {
      await sendMessage(env, chatId,
        "📚 <b>Canvas Reminder Bot</b>\n\n" +
        "Commands:\n" +
        "/remind — get your full assignment digest now\n" +
        "/grades — see all your current grades\n" +
        "/help   — show this message"
      );
    }

    return new Response("OK");
  },
};

// ── Canvas ────────────────────────────────────────────────────────────────────

async function buildGradesMessage(env) {
  const domain  = env.CANVAS_DOMAIN.replace(/^https?:\/\//, "").replace(/\/$/, "");
  const ignored = (env.IGNORED_COURSES || "PE,Stagecraft")
    .split(",")
    .map(s => s.trim().toLowerCase().replace(/\s+/g, ""));

  let courses;
  try {
    const url = `https://${domain}/api/v1/courses?enrollment_state=active&state[]=available&include[]=total_scores&per_page=50`;
    const resp = await fetch(url, {
      headers: { Authorization: `Bearer ${env.CANVAS_TOKEN}` },
    });
    if (!resp.ok) throw new Error(`Canvas returned ${resp.status}`);
    courses = await resp.json();
  } catch (e) {
    return `❌ Couldn't fetch grades: ${e.message}`;
  }

  const graded = courses
    .filter(c => c.id && c.name)
    .filter(c => {
      const name        = c.name.toLowerCase();
      const nameNoSpace = name.replace(/\s+/g, "");
      return !ignored.some(ig => name.includes(ig) || nameNoSpace.includes(ig));
    })
    .map(c => {
      const enr = (c.enrollments || []).find(e => e.type === "student");
      return {
        name:  c.name,
        score: enr?.computed_current_score ?? null,
        grade: enr?.computed_current_grade ?? null,
      };
    })
    .filter(c => c.score !== null)
    .sort((a, b) => a.score - b.score); // worst grade first so you see what needs work

  if (graded.length === 0) {
    return "📊 No graded courses found right now.";
  }

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

// ── Telegram ──────────────────────────────────────────────────────────────────

async function sendMessage(env, chatId, text) {
  await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML" }),
    }
  );
}
