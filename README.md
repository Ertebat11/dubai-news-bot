# Dubai Magazine Telegram News Bot

This bot scans Dubai/UAE news feeds, scores the newest stories for magazine value, deduplicates what it has already sent, and posts the best items to a Telegram chat or private channel.

It now groups the same story across multiple sources, boosts stories that appear on more than one outlet, supports breaking alerts plus digest/report mode, adds caption-ready summaries, short Farsi briefs, and one-line post ideas, extracts story images, adds Telegram feedback/approval buttons, saves forwarded Instagram/TikTok/X links as social leads, tracks watchlist topics, and sends daily intelligence reports.

## Current sources

- Gulf News UAE: page extraction from `https://gulfnews.com/uae`
- Khaleej Times UAE: page extraction from `https://www.khaleejtimes.com/uae`
- The National UAE: page extraction from `https://www.thenationalnews.com/uae/`
- ARN News Centre UAE: RSS from `https://www.arnnewscentre.ae/news/uae/feed.xml`
- Barq UAE Arabic: RSS from `https://www.uaebarq.ae/ar/feed/`
- Lovin Dubai: page extraction from `https://dubai.lovin.co/`
- Dubai One/DMI lane: Emirates 24|7 UAE RSS from `https://www.emirates247.com/rss/mobile/v2/uae.rss`

Some of these outlets do not publish clean category RSS feeds, so the bot supports both RSS and page extraction.

## Can this be free?

Yes, for a useful MVP:

- Telegram Bot API is free to use.
- RSS/news feeds and public news pages are usually free for a lightweight personal alert bot.
- GitHub Actions can run jobs for free in public repositories, with limits.
- Cloudflare Workers Cron can trigger the GitHub job on a more reliable free schedule.

The hard part is Instagram. Public Instagram scraping is unreliable and can violate platform terms. A safer approach is:

- Use official Meta/Instagram APIs only where you have permission.
- Track Instagram manually by letting your wife forward interesting post links to the bot.
- Later add an approved Instagram/Meta integration or a paid social-listening API.

## Setup

1. Create a bot in Telegram with `@BotFather`.
2. Copy `.env.example` to `.env` locally and fill `TELEGRAM_BOT_TOKEN`.
3. Send `/start` to the bot from the Telegram chat that should receive alerts.
4. Run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python bot.py --discover-chat
```

5. Put the printed chat id into `.env` as `TELEGRAM_CHAT_ID`.
6. Test without sending:

```bash
python bot.py --dry-run
```

7. Send real alerts:

```bash
python bot.py
```

Useful commands:

```bash
python bot.py --mode breaking --dry-run
python bot.py --mode digest --dry-run
python bot.py --mode heartbeat --dry-run
python bot.py --process-updates
```

In Telegram, send `/help`, `/status`, or `/sources`. Forward an Instagram, TikTok, or X link to the bot and it will save it as a social lead.

Digest commands:

```text
/digest
/digest lifestyle
/digest viral
/digest crime
/digest rules
/digest weather
/digest business
/saved
/delete saved 3
/trends
/report
/calendar
/watch rents
/watchlist
/unwatch rents
```

Every alert includes story links. Single alerts can include up to four source links when multiple outlets are covering the same story; digest entries include the lead source link.
When the source exposes an image, the bot sends the image before the full alert and includes the image URL in the story message.
Every story also includes a Farsi brief with the headline, story context, and a suggested caption angle.

## Free deployment with GitHub Actions

1. Push this folder to a GitHub repository.
2. In repository settings, add Actions secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Enable Actions.
4. The workflow in `.github/workflows/news-bot.yml` sends breaking alerts and processes feedback/social links when it is triggered manually or by Cloudflare.
5. The workflow in `.github/workflows/daily-digest.yml` sends a digest at about 10:07 AM and 7:07 PM Dubai time.
6. The workflow in `.github/workflows/daily-report.yml` sends a daily intelligence report at about 8:37 AM Dubai time.
7. The workflow in `.github/workflows/daily-heartbeat.yml` sends one daily alive/status message at about 1:17 PM Dubai time even if no breaking alert was sent.

## Reliable free breaking-alert schedule with Cloudflare

GitHub scheduled workflows can run late or be skipped during high load. For breaking alerts, use the Cloudflare Worker in `cloudflare-worker/` to trigger GitHub Actions instead.

The Worker runs at minute `13` and `43` every hour:

```text
13,43 * * * *
```

Setup summary:

1. Create a GitHub fine-grained personal access token for `Ertebat11/dubai-news-bot`.
2. Give it `Actions: Read and write`.
3. Deploy `cloudflare-worker/` with Wrangler.
4. Add the token as the Cloudflare Worker secret `GITHUB_TOKEN`.
5. Add any long random password as `RUN_SECRET`.

See `cloudflare-worker/README.md` for the exact commands.

Free editorial mode:

- The workflows use `AI_REWRITE=0`.
- No DeepSeek, OpenAI, or paid AI key is required.
- Captions, one-line post ideas, summaries, digests, and reports are generated by built-in templates.
- Farsi briefs are generated by built-in templates too, with no paid translation API.
- If you previously added a `DEEPSEEK_API_KEY` secret, remove it from GitHub so it cannot be used by accident.

For a private repository, GitHub Actions has monthly free-minute limits. For a public repository, standard GitHub-hosted runner usage is free, but keep secrets private and never commit your Telegram token.

## Tuning the magazine voice

Edit `feeds.yaml`:

- Add feeds under `sources`.
- Raise `weight` for sources you trust most.
- Add English or Arabic keywords under `keywords`.
- Increase `MIN_SCORE` if the bot sends too much.
- Decrease `MIN_SCORE` if it misses interesting stories.

The scoring is intentionally editorial: fresh + Dubai/UAE + attention signals + topic keywords.

Stories get an extra boost when multiple sources cover the same subject, including Arabic/English matches such as Barq plus Gulf News/ARN coverage of the same event.

Watchlist terms add an extra ranking boost. For example, `/watch rents` makes rent stories more likely to appear near the top of alerts, digests, and the daily report.

## Instagram lane

For now, do not run username/hashtag scraping from this bot. The compliant MVP is news/page monitoring plus manual social forwarding. When your wife forwards an Instagram/TikTok/X link to the bot, it stores it in `saved_links` for review.
