# Implementation plan — Meta ad verification dashboard

## Product and implementation approach

Continue the supplied Python/Flask/Jinja application rather than replacing its stack. Preserve the scanner/report pipeline and connect a redesigned, responsive dashboard to the existing job, progress, report, settings, and log APIs. Fix frontend fields that are currently not consumed by the backend, remove estimated-rate claims that are not measured, and present each result with its source, check time, freshness, and verification state.

Benchmark public product positioning from WinningHunter, Minea, and BigSpy. Use familiar research affordances—ad discovery, competitor context, filtering, saved/recent activity, and trend/status summaries—without copying their branding or presenting features the supplied backend does not provide.

Improve the verifier around truthful progress and reliability: validate dates and scope, deduplicate queue work, reuse fresh cache entries, cap concurrency, preserve confirmed answers, use bounded retries and respectful exponential cooldowns, and never convert a block, timeout, missing result, or approximate Meta counter into an authoritative exact number. Default to all imported page IDs for the selected countries and saved period; allow an explicit reportable-store-only scope. Do not rotate Tor circuits or proxies to evade Meta rate limits. Use the public Ad Library without Facebook identity; an official API path is out of scope because it requires account authorization and does not cover all commercial-ad countries.

Automatic continuation should not stop because arbitrary wall-clock, round-count, or no-progress limits were reached. It should continue only while Meta is answering and the run remains healthy; explicit user stop, completion, unrecoverable app/runtime failures, or a clear source refusal remain stop/pause conditions. After a source block, wait with increasing cooldown and report the reason; do not claim that an endless loop can force access.

Keep the existing filesystem-oriented scanner compatible. Build and package a self-hosted Docker Compose version, with persistent Docker volumes, health checks, secret-safe configuration, and required login when network-exposed. The user explicitly approved self-hosted packaging and said not to publish; do not publish the file-backed application on managed infrastructure with ephemeral container storage as if its data were durable.

## Project structure

- `app.py`: Flask pages, API routes, auth, health/readiness, progress reporting.
- `webapp/runner.py`: one-job manager, job lifecycle, pipeline subprocess orchestration, progress collection.
- `webapp/tor.py`, `verify_active.py`: public Ad Library access, safe concurrency/backoff, verification cache.
- `run_weekly.py`, `wh_scan.py`, `meta_api.py`, `scoring.py`, `chain_control.py`: existing data collection, normalization, report, and continuation pipeline.
- `templates/`: responsive dashboard, run detail, reports, settings.
- `static/`: design system and dashboard behavior.
- `tests/`: regression tests, including the fake-runner web integration tests.
- `results/`, `data/`: existing reports, cache, job history, and user settings; preserve through deployment volumes.
- `Dockerfile`, `docker-compose.yml`, `.env.example`: production-oriented local container setup and deployment guidance.

## Current continuation: activity memory and incremental reports

Use the existing filesystem persistence model rather than adding paid services or a database. Store a bounded, timestamped `events` list in each job's existing `job.json`; include job accepted/started/ended, round transitions, country completion/checkpoint, rate-limit cooldown, and report-save events. Expose a read-only activity endpoint and a dedicated timeline page linking back to run details. Keep the existing one-active-job contract explicit: reject a second start with a clear busy response instead of silently creating a queue that can stall.

After each country scan/export finishes, merge only country workbooks that are actually present and atomically save the current HTML, XLSX, and manifest. Preserve the full intended country set in metadata and list not-yet-finished/failed countries as partial, so an incremental snapshot never looks like a completed multi-country run. Protect concurrent country completions with a process-local lock; keep the prior valid report when no trusted Meta answer exists. Later country completions/retries replace the snapshot with a newer one.

Performance work stays within the current two-tab/two-country caps, fresh-cache behavior, and steady route policy. Deduplicate repeated `(page_id, country)` targets before network access. Do not claim 100 pages/minute or bypass Meta rate controls; show only measured answers/minute. No new paid service, Meta token, account login, or identity-bearing route is introduced.

## Design

Three styles considered: *Research ledger* (probability 0.061), *Signal-room dashboard* (0.074), and *Quiet editorial analytics* (0.039). Choose **Signal-room dashboard**.

- **Design Movement:** contemporary data-journalism control room with a restrained neo-brutalist information hierarchy.
- **Core Principles:** evidence before decoration; current status is visible at a glance; dense data remains scannable; every important action gives a clear result.
- **Color Philosophy:** deep ink and warm paper support long research sessions; a vivid chartreuse signal marks progress and primary actions; amber and red are reserved for caution and failure.
- **Layout Paradigm:** left-aligned workspace with a slim top command rail, a prominent live-verification strip, then a flexible evidence table and secondary detail panels.
- **Signature Elements:** a compact “verification pulse” progress strip; source/freshness chips; monospaced numeric counters paired with human-readable labels.
- **Interaction Philosophy:** preserve user control; stop is always available; do not hide uncertainty behind animation or guessed ETAs.
- **Animation:** subtle 120–180 ms transitions for panel/state changes; no pulsing alert except a running indicator; respect reduced-motion preferences.
- **Typography System:** system sans for navigation/body and a tabular-numeric monospace for counts, rates, timestamps, and IDs; clear 12/14/18/28 px hierarchy.
- **Brand Essence:** a private research console for checking competitor-page activity against Meta’s public source; direct, evidence-led, calm.
- **Brand Voice:** short and explicit. Example: “Counts Meta returned—not estimates.” “Unanswered pages stay marked unverified.”
- **Wordmark & Logo:** original Signal Desk mark formed from two offset ledger brackets around a signal dot; no competitor logos/assets.
- **Signature Brand Color:** signal chartreuse, used sparingly for the primary action and confirmed live progress.

## Material constraints

The 100 pages/min rate is a performance goal, not a guarantee: Meta controls the public site and may throttle or block requests. The interface must show measured successful verifications per minute, not an assumed target rate. The Meta website may display rounded counts; reports must describe them as the value shown by Meta, not “exact.” Verification can stay at $0 in WinningHunter credits, but estimate/scan modes consume WinningHunter API credits and hosting/network costs cannot be guaranteed to be zero. This build must not require or expose the client’s Facebook identity.
