/**
 * Cloudflare Worker — Canvas Reminder Telegram Bot
 *
 * Receives Telegram webhook updates and triggers the GitHub Actions
 * daily_reminder workflow when the user sends /remind.
 *
 * Required environment variables (set in Cloudflare dashboard):
 *   TELEGRAM_TOKEN    — bot token from @BotFather
 *   TELEGRAM_CHAT_ID  — your numeric chat ID
 *   GITHUB_TOKEN      — PAT with `workflow` scope
 *   GITHUB_OWNER      — your GitHub username
 *   GITHUB_REPO       — repository name (canvas-agent)
 */

export default {
  async fetch(request, env) {
    // Only accept POST requests from Telegram
    if (request.method !== "POST") {
      return new Response("OK");
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("OK");
    }

    const message = body?.message;
    if (!message) return new Response("OK");

    const text   = (message?.text || "").trim();
    const chatId = String(message?.chat?.id);

    // Security: ignore anyone who isn't you
    if (chatId !== env.TELEGRAM_CHAT_ID) {
      return new Response("OK");
    }

    if (text.startsWith("/remind")) {
      const ok = await triggerWorkflow(env);
      await sendMessage(env, chatId,
        ok
          ? "⏳ On it! Your assignment digest will arrive in ~30 seconds."
          : "❌ Couldn't trigger the reminder — check that your GITHUB_TOKEN is still valid."
      );

    } else if (text.startsWith("/start") || text.startsWith("/help")) {
      await sendMessage(env, chatId,
        "📚 <b>Canvas Reminder Bot</b>\n\n" +
        "Commands:\n" +
        "/remind — fetch your assignment digest right now\n" +
        "/help   — show this message"
      );
    }

    return new Response("OK");
  },
};

// ── Helpers ───────────────────────────────────────────────────────────────────

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

async function sendMessage(env, chatId, text) {
  await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id:    chatId,
        text,
        parse_mode: "HTML",
      }),
    }
  );
}
