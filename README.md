# 🎋 Bamboo Housing Watcher

Checks your Bamboo Housing searches every 15 minutes and pings you on Discord (or email) when a new listing appears — with price, address, and bed : bath ratio.

```
🏠 2 new listing(s) for winter-sublet
┌ W27 Larger Bedroom in 2B1B (FEMALE)
│ 💰 $1,215/month   📍 311 Batavia Pl, Waterloo
│ 🛏️ : 🛁  2 bed : 1 bath (2 bed per bath)
└ Rooms available 1 of 2 · Female Only
```

---

## Setup (≈10 min)

### 1. Make a Discord webhook
- In your server: **Server Settings → Integrations → Webhooks → New Webhook**
- Pick the channel, then **Copy Webhook URL**
- No bot or code needed — a webhook can post to a channel on its own

### 2. Put this folder on GitHub
- Create a new repo and push these files
- **Public repo** → free at any frequency
- **Private repo** → change the cron in `.github/workflows/watch.yml` to every 30 min to stay within free Actions minutes

### 3. Add the secret
- Repo → **Settings → Secrets and variables → Actions → New repository secret**
- Name: `DISCORD_WEBHOOK_URL`, value: the URL from step 1

### 4. Start it
- **Actions** tab → enable workflows → **Bamboo watcher → Run workflow**
- You'll get a "👀 Now watching…" message. From then on, only *new* listings ping you.

---

## Changing what you search for

Everything lives in `config.yaml`:

1. Set your filters on bamboohousing.ca
2. Copy the URL from the address bar
3. Paste it as a new block under `searches:` (or replace the existing `url`)

```yaml
  - name: fall-12-month
    url: "https://bamboohousing.ca/homepage?StartTerm=Fall&...&Sort=Recent"
    enabled: true
    max_price: 1100
    exclude_keywords: ["basement"]
```

Tips:
- Keep `Sort=Recent` in the URL — the watcher reads page 1, so newest-first matters
- A new `name` starts fresh (seeds quietly, then alerts on new posts)
- Flip `enabled: false` to pause a search without deleting it

---

## Email instead of / as well as Discord

Set `notify.email.enabled: true` in `config.yaml` and add these secrets:

| Secret | Gmail example |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `you@gmail.com` |
| `SMTP_PASSWORD` | a Gmail **App Password** (Google Account → Security → App passwords), not your real password |
| `EMAIL_TO` | where alerts go |

---

## How the bed : bath ratio is found

Bamboo's listing cards show "1 Room of 5" (→ a 5-bedroom unit) but not bathrooms, so the watcher:

1. Reads the title for patterns like `2 bed 1 bath`, `5bed/5bath`, `2B1B`, `two bed two bath`
2. Flags `private bathroom` / `ensuite` / `Private Bed & Bath`
3. For new listings with a link, opens the listing page and looks for a bathroom count there

If none of that finds it, the alert says "bathroom count not listed".

---

## Running locally (optional)

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

python watcher.py --dry-run       # see what it finds, sends nothing
python watcher.py --test-notify   # send a fake alert to check Discord
python watcher.py                 # real run
```

To run it from your own machine instead of GitHub, add a cron line (`crontab -e`):
```
*/15 * * * * cd /path/to/bamboo-watch && /usr/bin/python3 watcher.py >> watch.log 2>&1
```

---

## If it breaks

If Bamboo changes their page layout, the watcher finds 0 listings and sends **one** ⚠️ warning (not one every 15 min).

To debug:
```bash
python watcher.py --dry-run --dump-html     # saves the page to debug/winter-sublet.html
python watcher.py --from-file debug/winter-sublet.html
```

How it finds cards: it locates every element that reads exactly `$X/month`, grows outward to the largest container holding only that one price, and reads price → address → title → "N Rooms of M · Coed" from it. Company "Building" ads ("Starting From…") are skipped.

Be a good neighbour: one page request per search per run is plenty — don't crank the schedule below ~10 minutes.
