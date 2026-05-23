/**
 * Cloudflare Worker — Canvas Reminder Telegram Bot
 *
 * Commands:
 *   /remind              — triggers the full assignment digest
 *   /grades              — current grades for all classes
 *   /due                 — assignments due today & tomorrow
 *   /due <day>           — assignments due on a specific day (e.g. /due friday)
 *   /watched             — show which classes are being watched for new assignments
 *   /watch <class>       — add a class to the watch list
 *   /watch all           — watch all classes
 *   /unwatch <class>     — remove a class from the watch list
 *   /help                — list all commands
 *
 * Required environment variables (Cloudflare → Worker → Settings → Variables):
 *   TELEGRAM_TOKEN    TELEGRAM_CHAT_ID
 *   GITHUB_TOKEN  GITHUB_OWNER  GITHUB_REPO
 *   CANVAS_TOKEN  CANVAS_DOMAIN
 *   IGNORED_COURSES   (e.g. "PE,Stagecraft")
 */

const DAYS       = ["sunday","monday","tuesday","wednesday","thursday","friday","saturday"];
const SHORT_DAYS = ["sun","mon","tue","wed","thu","fri","sat"];

// ── Entry point ───────────────────────────────────────────────────────────────

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

    const spaceIdx = text.indexOf(" ");
    const cmd      = (spaceIdx === -1 ? text : text.slice(0, spaceIdx)).toLowerCase();
    const arg      = spaceIdx === -1 ? "" : text.slice(spaceIdx + 1).trim();

    if (cmd === "/remind") {
      const ok = await triggerWorkflow(env);
      await sendMessage(env, chatId,
        ok ? "⏳ On it! Your digest will arrive in ~30 seconds."
           : "❌ Couldn't trigger the reminder — check your GITHUB_TOKEN.");

    } else if (cmd === "/grades") {
      await sendMessage(env, chatId, "⏳ Fetching grades…");
      await sendMessage(env, chatId, await buildGradesMessage(env));

    } else if (cmd === "/due") {
      const target = parseTarget(arg.toLowerCase());
      if (!target) {
        await sendMessage(env, chatId,
          "❓ Didn't recognise that day.\n\nExamples:\n/due\n/due tomorrow\n/due monday");
      } else {
        await sendMessage(env, chatId, `⏳ Checking assignments for ${target.label}…`);
        await sendMessage(env, chatId, await buildDueMessage(env, target));
      }

    } else if (cmd === "/watched") {
      await sendMessage(env, chatId, await buildWatchedMessage(env));

    } else if (cmd === "/watch") {
      await sendMessage(env, chatId, await handleWatch(env, arg));

    } else if (cmd === "/unwatch") {
      await sendMessage(env, chatId, await handleUnwatch(env, arg));

    } else if (cmd === "/start" || cmd === "/help") {
      await sendMessage(env, chatId,
        "📚 <b>Canvas Reminder Bot</b>\n\n" +
        "<b>Assignments</b>\n" +
        "/remind        — full digest now\n" +
        "/due           — due today &amp; tomorrow\n" +
        "/due friday    — due on a specific day\n\n" +
        "<b>Grades</b>\n" +
        "/grades        — all current grades\n\n" +
        "<b>Notifications</b>\n" +
        "/watched             — see watch list\n" +
        "/watch Biology       — add a class\n" +
        "/watch all           — watch everything\n" +
        "/unwatch Biology     — stop watching a class\n" +
        "/unwatch all         — go back to watching everything\n\n" +
        "/help          — this message"
      );
    }

    return new Response("OK");
  },
};

// ── Watch list commands ───────────────────────────────────────────────────────

async function buildWatchedMessage(env) {
  const [watched, courses] = await Promise.all([
    getWatchedClasses(env),
    fetchCourses(env),
  ]);

  const ignored  = parseIgnored(env);
  const active   = (courses || [])
    .filter(c => c.id && c.name && !isIgnored(c.name, ignored))
    .map(c => c.name);

  let msg = "👁 <b>Assignment Watch List</b>\n\n";

  if (watched.length === 0) {
    msg += "Watching: <b>all classes</b>\n\n";
  } else {
    msg += "Watching:\n" + watched.map(w => `  • ${w}`).join("\n") + "\n\n";
  }

  msg += "<b>Your active classes:</b>\n" + active.map(n => `  ${n}`).join("\n");
  msg += "\n\n<i>Use /watch &lt;name&gt; or /unwatch &lt;name&gt; to change the list.\nUse /watch all to watch every class.</i>";
  return msg;
}

async function handleWatch(env, arg) {
  if (!arg) return "Usage: /watch &lt;class name&gt;   or   /watch all";

  if (arg.toLowerCase() === "all") {
    await setWatchedClasses(env, []);
    return "✅ Now watching <b>all classes</b> for new assignments.";
  }

  const current = await getWatchedClasses(env);
  const already = current.some(w => w.toLowerCase() === arg.toLowerCase());
  if (already) return `ℹ️ Already watching <b>${arg}</b>.`;

  const updated = [...current, arg];
  await setWatchedClasses(env, updated);
  return (
    `✅ Added <b>${arg}</b> to watch list.\n\n` +
    `Now watching:\n` + updated.map(w => `  • ${w}`).join("\n")
  );
}

async function handleUnwatch(env, arg) {
  if (!arg) return "Usage: /unwatch &lt;class name&gt;   or   /unwatch all";

  if (arg.toLowerCase() === "all") {
    await setWatchedClasses(env, []);
    return "✅ Cleared watch list. Now watching <b>all classes</b>.";
  }

  const current = await getWatchedClasses(env);
  const updated = current.filter(w => w.toLowerCase() !== arg.toLowerCase());

  if (updated.length === current.length) {
    return `ℹ️ <b>${arg}</b> wasn't in your watch list.`;
  }

  await setWatchedClasses(env, updated);

  if (updated.length === 0) {
    return `✅ Removed <b>${arg}</b>. Now watching <b>all classes</b>.`;
  }
  return (
    `✅ Removed <b>${arg}</b>.\n\nNow watching:\n` +
    updated.map(w => `  • ${w}`).join("\n")
  );
}

// ── GitHub Variables API ──────────────────────────────────────────────────────

async function getWatchedClasses(env) {
  const resp = await fetch(
    `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/variables/WATCHED_CLASSES`,
    { headers: ghHeaders(env) }
  );
  if (resp.status === 404) return [];          // variable doesn't exist yet = watch all
  if (!resp.ok) throw new Error(`GitHub ${resp.status}`);
  const { value } = await resp.json();
  return value ? value.split(",").map(s => s.trim()).filter(Boolean) : [];
}

async function setWatchedClasses(env, classes) {
  const value = classes.join(",");
  const url   = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/variables`;

  // Try PATCH (update) first; if 404 the variable doesn't exist yet → POST (create)
  const patch = await fetch(`${url}/WATCHED_CLASSES`, {
    method:  "PATCH",
    headers: { ...ghHeaders(env), "Content-Type": "application/json" },
    body:    JSON.stringify({ name: "WATCHED_CLASSES", value }),
  });

  if (patch.status === 404) {
    await fetch(url, {
      method:  "POST",
      headers: { ...ghHeaders(env), "Content-Type": "application/json" },
      body:    JSON.stringify({ name: "WATCHED_CLASSES", value }),
    });
  }
}

function ghHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept:        "application/vnd.github.v3+json",
    "User-Agent":  "canvas-telegram-bot",
  };
}

// ── GitHub Actions ────────────────────────────────────────────────────────────

async function triggerWorkflow(env) {
  const resp = await fetch(
    `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}` +
    `/actions/workflows/daily_reminder.yml/dispatches`,
    {
      method:  "POST",
      headers: { ...ghHeaders(env), "Content-Type": "application/json" },
      body:    JSON.stringify({ ref: "main" }),
    }
  );
  return resp.ok;
}

// ── /grades ───────────────────────────────────────────────────────────────────

async function buildGradesMessage(env) {
  let courses;
  try {
    courses = await canvasGet(env,
      `/courses?enrollment_state=active&state[]=available&include[]=total_scores&per_page=50`);
  } catch (e) { return `❌ Couldn't fetch grades: ${e.message}`; }

  const ignored = parseIgnored(env);
  const graded  = courses
    .filter(c => c.id && c.name && !isIgnored(c.name, ignored))
    .map(c => {
      const enr = (c.enrollments || []).find(e => e.type === "student");
      return { name: c.name, score: enr?.computed_current_score ?? null, grade: enr?.computed_current_grade ?? null };
    })
    .filter(c => c.score !== null)
    .sort((a, b) => a.score - b.score);

  if (graded.length === 0) return "📊 No graded courses found.";

  const lines = ["📊 <b>Current Grades</b>  (lowest first)\n"];
  for (const c of graded) {
    const emoji    = c.score >= 90 ? "🟢" : c.score >= 80 ? "🟡" : c.score >= 70 ? "🟠" : "🔴";
    const gradeStr = c.grade ? ` (${c.grade})` : "";
    lines.push(`${emoji} <b>${c.score.toFixed(1)}%${gradeStr}</b> — ${c.name}`);
  }
  return lines.join("\n");
}

// ── /due ──────────────────────────────────────────────────────────────────────

function parseTarget(arg) {
  const todayUTC = new Date();
  todayUTC.setUTCHours(0, 0, 0, 0);

  if (!arg || arg === "today") {
    const tomorrow = new Date(todayUTC);
    tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
    return { label: "Today & Tomorrow", dates: [todayUTC, tomorrow] };
  }
  if (arg === "tomorrow") {
    const t = new Date(todayUTC);
    t.setUTCDate(t.getUTCDate() + 1);
    return { label: "Tomorrow", dates: [t] };
  }
  let idx = DAYS.indexOf(arg);
  if (idx === -1) idx = SHORT_DAYS.indexOf(arg);
  if (idx === -1) return null;

  let daysUntil = idx - todayUTC.getUTCDay();
  if (daysUntil <= 0) daysUntil += 7;
  if (daysUntil === 7) daysUntil = 0;
  const target = new Date(todayUTC);
  target.setUTCDate(todayUTC.getUTCDate() + daysUntil);
  const label = DAYS[idx].charAt(0).toUpperCase() + DAYS[idx].slice(1);
  return { label, dates: [target] };
}

async function buildDueMessage(env, target) {
  const ignored   = parseIgnored(env);
  const targetSet = new Set(target.dates.map(d => d.toISOString().slice(0, 10)));

  let courses;
  try { courses = await fetchCourses(env); }
  catch (e) { return `❌ Couldn't fetch courses: ${e.message}`; }

  const active = courses
    .filter(c => c.id && c.name && !isIgnored(c.name, ignored));

  const lists = await Promise.all(
    active.map(course =>
      canvasGet(env, `/courses/${course.id}/assignments?order_by=due_at&per_page=50`)
        .then(list =>
          list
            .filter(a => a.due_at && targetSet.has(a.due_at.slice(0, 10)))
            .map(a => ({
              name: a.name, course: course.name,
              due_at: new Date(a.due_at), points: a.points_possible,
              url: a.html_url || "", submitted: !!a.has_submitted_submissions,
            }))
        ).catch(() => [])
    )
  );

  const assignments = lists.flat().sort((a, b) => a.due_at - b.due_at);

  if (assignments.length === 0) {
    const when = target.label === "Today & Tomorrow" ? "today or tomorrow" : `on ${target.label}`;
    return `✅ Nothing due ${when} — enjoy the free time!`;
  }

  const lines = [`📅 <b>Due ${target.label}</b> — ${assignments.length} assignment(s)\n`];
  const byDate = {};
  for (const a of assignments) {
    const key = a.due_at.toISOString().slice(0, 10);
    (byDate[key] = byDate[key] || []).push(a);
  }
  for (const [key, items] of Object.entries(byDate)) {
    if (Object.keys(byDate).length > 1) {
      const d = new Date(key + "T12:00:00Z");
      lines.push(`\n<b>${d.toLocaleDateString("en-US", { weekday:"long", month:"short", day:"numeric", timeZone:"UTC" })}</b>`);
    }
    for (const a of items) {
      const time  = a.due_at.toLocaleTimeString("en-US", { hour:"numeric", minute:"2-digit", timeZone:"America/Los_Angeles" });
      const pts   = a.points ? `  •  ${Math.round(a.points)} pts` : "";
      const check = a.submitted ? "✅ " : "";
      lines.push(`${check}<b>${a.name}</b>${pts}\n     📖 ${a.course}  ·  ⏰ ${time}\n     🔗 <a href="${a.url}">Open in Canvas</a>`);
    }
  }
  return lines.join("\n");
}

// ── Canvas helpers ────────────────────────────────────────────────────────────

async function fetchCourses(env) {
  return canvasGet(env, `/courses?enrollment_state=active&state[]=available&per_page=50`);
}

async function canvasGet(env, path) {
  const domain = env.CANVAS_DOMAIN.replace(/^https?:\/\//, "").replace(/\/$/, "");
  const resp   = await fetch(`https://${domain}/api/v1${path}`, {
    headers: { Authorization: `Bearer ${env.CANVAS_TOKEN}` },
  });
  if (!resp.ok) throw new Error(`Canvas API error ${resp.status}`);
  return resp.json();
}

// ── Shared helpers ────────────────────────────────────────────────────────────

function parseIgnored(env) {
  return (env.IGNORED_COURSES || "PE,Stagecraft")
    .split(",").map(s => s.trim().toLowerCase().replace(/\s+/g, ""));
}

function isIgnored(name, ignored) {
  const n = name.toLowerCase(), ns = n.replace(/\s+/g, "");
  return ignored.some(ig => n.includes(ig) || ns.includes(ig));
}

async function sendMessage(env, chatId, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
}
