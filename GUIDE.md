# Signal Desk operator guide

## What the system does

Signal Desk keeps the supplied Python/Flask scanner and report pipeline, with a new dashboard connected to its job, progress, report, settings, and log endpoints. WinningHunter data is used to discover competitor pages; the Meta public Ad Library is used to check the active-ad result shown for each imported page ID.

There is no Facebook login, Facebook account token, or client identity in the verification flow. The official Ad Library API is not used. Verification reads the public website through Playwright, running Meta's scripts while aborting image, media, font, and stylesheet requests. Direct host routing is the default; a steady Tor route remains an optional setting. Tor exit/circuit rotation and paid-proxy rotation are deliberately disabled; this does not bypass Meta’s limits.

## Start and use

Use the [Docker Compose quick start](README.md#docker-compose-quick-start) for a persistent self-hosted install. The dashboard opens at the mapped host address (loopback port 8000 by default). Basic authentication uses any username plus `WH_WEB_PASSWORD`.

1. Open **Market desk**. Existing imported data appears in the coverage panel and competitor table.
2. Choose **Verify saved pages** to check data already on this machine. **All imported page IDs in the selected countries and saved period** is the default; the optional reportable-store scope is narrower. The country field defaults to the latest report’s countries; add every country you want included.
3. Leave the round time limit at zero for no per-round wall-clock limit. Automatic continuation is enabled by default for verification. A user can always press **Stop run**.
4. Watch the run monitor or open **Run detail** to read the actual status, log, rounds, and artifacts.
5. Open **Activity memory** for the timestamped job/action timeline; events are stored in each job record and link to **Run detail**.
6. Open **Reports** for saved HTML/XLSX output. Country snapshots are refreshed as each country export finishes; until all requested countries finish, the manifest marks the remaining countries partial.
7. Use **Settings** for verification freshness, safe worker concurrency, Tor selection, and optional WinningHunter scan credentials.

Only one job runs at a time. A second start request is rejected with a clear “not queued” message, rather than left invisibly waiting. Stop or wait for the active job before submitting another.

If there is no imported page data, verification has nothing to check. A WinningHunter **Scan** can import pages and may consume that account’s API credits. **Estimate** can also consume API credits. Neither is necessary to verify already-saved pages.

## Reading a result

- **Verified active:** Meta returned a positive active-ad counter for that page. The number is what the public page displayed when checked.
- **Verified inactive:** Meta’s active/all-ads views and a contemporaneous control answer supported a zero. This is not inferred from an empty page while Meta is blocked.
- **Unverified:** no trustworthy count was obtained.
- **Stale:** a prior answer is older than the configured freshness window and needs a new check.
- **Failed:** the application could not complete the check.
- A `~` prefix means Meta’s visible counter was rounded. The hidden underlying exact number is not known.

A verified page is a time-stamped reading of the public source, not a guarantee that its ads remain active later. Ads change, and Meta can change or round the counters. Check time and status should accompany any exported number.

## Throughput and continuation

The 100-pages-per-minute figure is a performance target only. No application can guarantee it against Meta’s public-site throttling, host CPU/RAM, or network conditions. Dashboard rates count successful answers only after they are measured; no guessed ETA is shown. Verification uses a global cap of five browser tabs and up to four tabs for one country, distributed fairly across selected countries. Settings allows 1–5 concurrent country workers. This bounds browser memory but does not prevent Meta throttling. Direct host routing is the default; Tor can add latency, and Meta may rate-limit either route.

Repeated `(page ID, country)` targets are collapsed before network access, and cached answers avoid repeat visits while fresh. A country workbook and per-run HTML/XLSX progress snapshot are refreshed at each 10% of distinct pages processed. The canonical report is only replaced when a trusted Meta answer is available; otherwise the prior valid canonical report is preserved. Reports refresh their file list every five seconds, and completed rounds are retained as run-by-run artifacts. Raising concurrency, rotating routes, or retrying more aggressively would not make a 100/min result truthful or reliable.

Automatic verification has no arbitrary wall-clock, round-count, or no-progress stop ceiling. Routine automatic re-checks use a 60-second interval. When Meta returns a rate-limit signal, the same unanswered pages are retained and retried after increasing cooldowns on the same route; this explicit source cooldown may exceed 60 seconds and is not bypassed. A run may therefore wait for a long time or may not complete while Meta refuses or blocks requests. This behavior does not promise that an endless run can force access. Stop a run if the source does not recover or the host needs to be reclaimed.

The optional built-in pre-run test suite is intended for development or upgrade checks, not routine production runs; it can add over a minute before verification starts. Leave it disabled in Settings when minimizing time-to-results.

## Access and credentials

`WH_API_KEY` is optional and only used for WinningHunter scan/estimate operations. The app never returns the full saved value to the settings page. Meta verification does not require a Meta token or Facebook identity.

Compose requires a non-empty `WH_WEB_PASSWORD`. The example binds to `127.0.0.1` on the host. For remote operation, keep a strong password, terminate TLS at a trusted reverse proxy, and configure firewall rules. Do not set the developer-preview bypass option in a self-hosted environment.

## Persistence, backup, and restore

Compose uses named volumes for `data/` (settings, run history, logs, scan cache), `results/` (reports, verification cache, imported scan state), and `.thumb_cache/` (reusable thumbnails). `docker compose down` keeps them; `docker compose down -v` removes them.

Activity events are kept in each `data/runs/<job-id>/job.json` (most recent 500 per job), and the timeline sorts by recorded start time. Incremental report files and manifests are atomically replaced under `results/`; the previous valid report remains when a country produces no trustworthy Meta answer. Completed round copies appear in Reports with their job ID and round number.

Back up the durable folders and the `.env` secret separately. One simple tar backup on Linux/macOS is:

```sh
mkdir -p backups
docker compose exec -T wh-scanner tar -czf - -C /app/data . > backups/data.tar.gz
docker compose exec -T wh-scanner tar -czf - -C /app/results . > backups/results.tar.gz
```

Restore only into the intended stack, with the same project volumes mounted. Stop the service first, then extract the archives into `/app/data` and `/app/results` as the container user.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `WH_WEB_PASSWORD` missing | Set a strong, non-empty value in `.env`, then rerun `docker compose up -d`. |
| Sign-in fails | Use any username and the exact password from `.env`; restart the service after changing environment values. |
| `WH_API_KEY is missing` | Only scan/estimate need the key. Use Verify/Rebuild for saved page data. |
| Tor not detected or did not bootstrap | Tor is optional. Install it on the host/container and set its executable path, or turn Tor off to use the ordinary route. |
| Meta rate limiting / run waiting | The app preserves unanswered pages and backs off. It will wait; it will not rotate identities to evade source limits. |
| No report or no competitor rows | Check **Run detail** and `docker compose logs -f wh-scanner`; verify that the selected period has imported data. |
| Host port already in use | Set `WH_PUBLIC_PORT` in `.env` to an available host port, for example `8010`. |
| Chromium launch failure | Rebuild with `docker compose up -d --build`, or install Chromium with `python -m playwright install chromium` for a local Python install. |
| Accidentally removed data | `docker compose down -v` deletes named volumes. Restore a prior backup if one exists. |

Run the deterministic offline checks from the source directory with `python -m pytest tests -q`. They do not contact Meta or WinningHunter.
