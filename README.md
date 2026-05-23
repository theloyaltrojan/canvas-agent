# 📚 Canvas Assignment Reminder Agent

A lightweight Python agent that fetches your upcoming Canvas assignments and sends a
formatted daily digest to **Telegram** every morning via **GitHub Actions** — no server needed.

---

## 🗂 Project Structure

```
canvas-agent/
├── canvas_agent.py                  # main script
├── requirements.txt
├── .github/
│   └── workflows/
│       └── daily_reminder.yml       # GitHub Actions schedule
└── README.md
```

---

## 🔧 One-Time Setup

### Step 1 — Get your Canvas API token

1. Log in to Canvas.
2. Go to **Account** (top-left avatar) → **Settings**.
3. Scroll to **Approved Integrations** → click **+ New Access Token**.
4. Give it a name like `Reminder Agent`, set an expiry if you like, and click **Generate Token**.
5. **Copy the token now** — Canvas won't show it again.

Your `CANVAS_DOMAIN` is the hostname in your Canvas URL, e.g. `canvas.myschool.edu` or
`myschool.instructure.com` (no `https://`).

---

### Step 2 — Create a Telegram bot

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts (pick any name/username).
3. BotFather gives you a **bot token** — save it.  It looks like `123456789:ABCdef…`
4. Start a chat with your new bot (search its username, click **Start**).
5. Get your **chat ID** by visiting this URL in your browser (replace `<TOKEN>` with yours):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   After sending any message to your bot, look for `"chat":{"id": 123456789}` — that number is your chat ID.

---

### Step 3 — Create the GitHub repo & add secrets

1. Create a **new GitHub repository** (can be private).
2. Push this project folder to it:
   ```bash
   cd canvas-agent
   git init && git add . && git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin main
   ```
3. In your repo on GitHub go to **Settings → Secrets and variables → Actions → New repository secret** and add these four secrets:

   | Secret name        | Value                                    |
   |--------------------|------------------------------------------|
   | `CANVAS_TOKEN`     | The token from Step 1                    |
   | `CANVAS_DOMAIN`    | e.g. `canvas.myschool.edu`               |
   | `TELEGRAM_TOKEN`   | The bot token from Step 2                |
   | `TELEGRAM_CHAT_ID` | Your chat ID from Step 2                 |

---

### Step 4 — Set your timezone

Open `.github/workflows/daily_reminder.yml` and adjust the `cron` line so the job fires at
8 AM **your** time:

| Timezone | 8 AM in UTC cron |
|----------|-----------------|
| EST (UTC-5) | `0 13 * * *` |
| CST (UTC-6) | `0 14 * * *` |
| MST (UTC-7) | `0 15 * * *` |
| PST (UTC-8) | `0 16 * * *` |
| EDT (UTC-4, summer) | `0 12 * * *` |
| CDT (UTC-5, summer) | `0 13 * * *` |

---

## ▶️ Test it immediately

After pushing, go to your repo → **Actions** → **Canvas Daily Reminder** → **Run workflow**.
You'll receive a Telegram message within ~30 seconds.

---

## 📬 Sample Message

```
📚 Daily Canvas Digest — Monday, May 26
Next 7 days · 3 assignment(s)

🔴 Due Today
  Programming HW #5  •  100 pts
     📖 CS 301 – Data Structures
     ⏰ 11:59 PM, May 26
     🔗 Open in Canvas

🟠 Due Tomorrow
  Reading Response 3
     📖 ENG 201 – Modern Literature
     ⏰ 9:00 AM, May 27
     🔗 Open in Canvas

🟡 This Week
  Lab Report 2  •  50 pts
     📖 BIO 101 – Intro Biology
     ⏰ 11:59 PM, May 30
     🔗 Open in Canvas
```

---

## ⚙️ Customisation

| Variable | Default | What it does |
|----------|---------|--------------|
| `DAYS_AHEAD` | `7` | How many days forward to look for assignments |
| `cron` in workflow | `0 13 * * *` | When the message is sent (UTC) |

Change `DAYS_AHEAD` in the workflow YAML env block without touching any code.
