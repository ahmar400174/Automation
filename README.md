# Signal Desk — self-hosted Meta page verification

Signal Desk is a private research workspace built from the supplied WinningHunter scanner project. It imports saved competitor-page data, checks page IDs against Meta’s public Ad Library, and presents reports, check times, and verification state in a responsive dashboard.

**It does not require a Facebook login or reveal a client identity.** A Meta result may be rounded; rounded values retain a `~` marker. Unknown, blocked, stale, failed, and confirmed inactive results are not converted into made-up counts.

## Docker Compose quick start

1. Install Docker Engine and the Docker Compose plugin.
2. Copy `.env.example` to `.env` and set a strong, non-empty `WH_WEB_PASSWORD`. Leave `WH_BIND=127.0.0.1` for local-only use. Add `WH_API_KEY` only if you need WinningHunter estimate or scan modes.
3. From this directory run:

   ```sh
   docker compose up -d --build
   ```

4. Open `http://localhost:8000` and sign in with any username and the password from `.env`.

The image includes Tor and Chromium. Tor is optional in Settings. The service binds inside the container, but the host port is bound to loopback by default. For remote access, use a trusted HTTPS reverse proxy, set a strong password, and deliberately set `WH_BIND=0.0.0.0`; do not expose plain HTTP Basic Auth directly to the internet.

The app stores its state in Docker-managed volumes (`wh-data`, `wh-results`, and `wh-thumbnails`). `docker compose down` preserves those volumes; `docker compose down -v` deletes them. Back up the volumes and the `.env` file securely before migrating or removing the stack. Report files can also be downloaded from the Reports page.

### Carry over existing scan data

The Compose volumes start empty. To reuse a prior installation’s saved reports, page IDs, and verification cache, start this stack, copy the old `results/` contents into the running service, then restart it:

```sh
docker compose cp /absolute/path/to/old/results/. wh-scanner:/app/results/
docker compose restart wh-scanner
```

Keep the old data private and back it up first. The archive’s unused `known_sheet.csv` contained specific competitor page IDs; it is intentionally not bundled as application source, and the app does not import that CSV automatically. A fresh install with no saved page data can verify nothing until prior scan data is copied in or a WinningHunter scan is run.

## Modes and cost boundary

- **Verify:** checks saved page IDs and uses no WinningHunter API credits. This is the default dashboard action. Verify and rebuild call the linked `meta_ads_verifier` package first (`python -m meta_ads_verifier`). It writes trusted counts into `results/verify_cache.json`. The browser verifier then checks only the pages that package could not read. Estimate mode does not call it.
- **Rebuild:** rebuilds report output from saved scan data; does not require a WinningHunter key.
- **Scan / Estimate:** require the client’s WinningHunter key and may consume that account’s API credits.

By default, Verify checks every imported page ID in the selected countries and saved period, with no arbitrary page cap. Country queues run under a global five-browser-tab cap, with up to four tabs for a single country; slots are fairly distributed across selected countries. The default route is the host's direct outbound network (Tor remains optional in Settings). Playwright continues to run Meta's JavaScript but aborts images, media, fonts and stylesheets. Country workbooks and per-run HTML/XLSX snapshots refresh every 10% of distinct pages processed; the Reports page refreshes its list every five seconds and retains completed run copies.

If Verify/Rebuild is started with dates that do not exist in the saved WinningHunter scan archive, the runner automatically reuses the newest scan period shared by the selected countries and clearly logs the period switch. It does not call the WinningHunter API for this recovery, and the generated report shows the archived period rather than pretending the data came from the requested date. If selected countries have no common archived period, the runner keeps the explicit missing-data diagnostic.

The application itself uses no paid API or Meta account for verification, but “$0 total cost” cannot be guaranteed: hosting, bandwidth, Tor availability, and the client’s WinningHunter account for Scan/Estimate are outside this application. The requested 100 pages/minute is a goal, not a promise. Meta controls the public site and can slow or block any network route. Routine automatic re-checks use a 60-second interval; explicit Meta cooldowns can take longer and are honored rather than bypassed. Automatic verification has no fixed total time, round, or no-progress ceiling by default; it can wait during source throttling until the source responds or a user stops it.

For production runs, leave the optional built-in test suite disabled in Settings; it is useful before upgrades but adds startup time. Tor is optional and may add latency; the direct host route may be faster, but Meta can still rate-limit it.

## Local Python development

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows:     .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m playwright install chromium
python app.py
```

The local Python server binds to loopback by default. Run the offline regression suite with `python -m pytest tests -q`. For a runtime-only install, use `requirements.txt`; the Docker image installs runtime packages only. For full operating instructions, see [GUIDE.md](GUIDE.md); architecture and safety behavior are in [ARCHITECTURE.md](ARCHITECTURE.md).
