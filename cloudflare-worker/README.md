# Cloudflare Scheduler

This Worker replaces GitHub's unreliable scheduled cron for breaking alerts.
Cloudflare runs the cron, then asks GitHub Actions to run `news-bot.yml`.

Flow:

```text
Cloudflare Cron -> GitHub workflow_dispatch -> python bot.py -> Telegram
```

## Schedule

The Worker runs at minute `13` and `43` every hour:

```text
13,43 * * * *
```

That means about `:13` and `:43` Dubai time every hour. Cloudflare cron uses UTC, but minutes stay the same.

## Required secret

Create a GitHub fine-grained personal access token:

- Repository access: `Ertebat11/dubai-news-bot`
- Permissions: `Actions` -> `Read and write`
- Expiration: choose the longest GitHub allows, then renew before it expires

Do not commit this token.

## Deploy

From this folder:

```bash
npx wrangler login
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put RUN_SECRET
```

When it asks for the `GITHUB_TOKEN`, paste the GitHub token.
For `RUN_SECRET`, type any long random password. It protects the manual `/run` URL.

The first deploy creates the Worker and cron trigger. The secrets are added after that.

## Test

After deploy, run:

```bash
curl https://YOUR-WORKER-NAME.YOUR-SUBDOMAIN.workers.dev/health
curl -X POST https://YOUR-WORKER-NAME.YOUR-SUBDOMAIN.workers.dev/run -H "x-run-secret: YOUR_RUN_SECRET"
```

The `/run` test should create a new GitHub Actions run for `Dubai news bot`.
