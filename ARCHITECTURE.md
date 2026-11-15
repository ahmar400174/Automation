# Signal Desk architecture and verification policy

## Components

- `app.py` serves the Jinja pages, HTTP Basic authentication, same-origin JSON endpoints, saved-report downloads, job status, and workbook-derived competitor rows.
- `templates/` and `static/` implement the Signal Desk dashboard, report browser, run detail, settings, responsive styles, filtering, polling, and live logs.
- `webapp/runner.py` validates run forms, allows one active job, starts the workflow subprocess, captures logs, reports progress, manages artifacts, and continues verification runs.
- `webapp/tor.py` starts one optional shared Tor SOCKS route. No exit pinning or rotation is attempted.
- `verify_active.py` reads the public Ad Library with Playwright, caches source-backed results, differentiates a positive result, confirmed zero, stale result, rate limit, and unknown/error, and uses respectful exponential cooldowns.
- `wh_scan.py` imports and normalizes WinningHunter records. `run_weekly.py` merges per-country workbook results and atomically refreshes the live HTML/XLSX reports and run manifest.
- Docker Compose persists `data/`, `results/`, and `.thumb_cache/` as named volumes.

## Verification semantics

A count is accepted only if the public source supplies a numeric result. A zero is accepted only after the configured active/all-ads checks and control-page logic support the zero rather than a general empty/error page. The visible Meta rounded marker is carried through the cache, spreadsheet, HTML report, and dashboard. No error, timeout, source block, stale value, or WinningHunter estimate is relabeled as a verified Meta count.

The dashboard’s page table is derived from the latest saved merged XLSX. It does not manufacture page rows. The coverage summary is computed from the latest run manifest, while live progress is taken from real verifier output. A successful pages/minute reading counts numeric answers only; it is withheld until a completed verification round has enough observations to be meaningful. No estimated completion time is presented.

## Concurrency and rate limits

Verification defaults to direct host routing, with Tor optional. Playwright remains responsible for JavaScript-rendered Meta counts; interception aborts images, media, fonts, and stylesheets but continues scripts and document requests. The scheduler uses at most five country workers and five Chromium tabs globally, with up to four tabs per country and fair distribution across selected countries. This bounds memory use but cannot promise a specific pages-per-minute rate. The application does not rotate Tor circuits, proxy identities, or IPs to evade Meta blocks. Sustained source-limit responses trigger an increasing cooldown, and unanswered pages remain eligible for retry.

Every ten percentage points of distinct pages processed, the country workbook is atomically updated and the parent runner refreshes a per-run HTML/XLSX progress snapshot and manifest. The reports index polls every five seconds. The canonical report is replaced only when it contains at least one trusted Meta answer; otherwise the previous valid canonical report is preserved. Completed rounds are copied into the associated run record and remain available in Reports.

The 100 pages/minute goal cannot be guaranteed. Real throughput is controlled by the Meta public site, source response time, country, Tor bootstrap/exit, host resources, and network. The application reports measured answers, not a simulated target.

## Continuation and failure boundaries

Verification defaults to every imported page ID in the selected countries and saved period, uses zero for the per-round time limit, and has no default total-run, automatic-round, or no-progress ceiling. Routine automatic re-checks use a 60-second interval; an explicit Meta cooldown can exceed that interval and is honored rather than bypassed. Continuation ends on completion, explicit user stop, or an application/runtime/source condition that cannot produce a report. Long runs are therefore resumable but cannot guarantee Meta will eventually answer.

## Cost and identity boundaries

Meta verification uses the public Ad Library website without a Meta API token, Facebook login, or client identity. It does not consume WinningHunter API credits. WinningHunter **Scan** and **Estimate** use the configured account’s API and may consume that account’s credits. Docker, the host, bandwidth, Tor, and upstream networks may have costs; the application cannot guarantee zero total operating cost.

## Persistence and security

For remote access, Compose requires `WH_WEB_PASSWORD`, binds the published host port to loopback unless explicitly changed, and uses persistent named volumes. Use a strong password and HTTPS through a trusted reverse proxy for any remote access. Store `.env` securely; do not commit it. Keep backups of data/results volumes before upgrades or migrations. No Manus-hosted deployment is configured or published for this filesystem-backed package.
