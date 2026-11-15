# Deployment guide (Signal Desk)

This is a **Python (Flask + Waitress) app**. There is no frontend build step (no `package.json`, no `npm run build`).
The "build" is the Docker image, which installs Python deps, Chromium (Playwright) and Tor.

> Not suitable for Vercel / serverless: it runs long background verification jobs, needs Chromium + Tor,
> and stores state on disk. Use Docker on a VPS, or Railway / Render / Fly.io (Docker deploy with a persistent volume).

## Option A - any VPS with Docker (recommended)
```sh
cp .env.example .env          # set a strong WH_WEB_PASSWORD (required); WH_API_KEY optional
docker compose up -d --build
```
Open http://SERVER_IP:8000 (default binds to 127.0.0.1; for public access put an HTTPS reverse proxy
such as Caddy/Nginx in front, or set WH_BIND=0.0.0.0 in .env behind a firewall).

## Option B - Railway / Render / Fly.io (Docker)
- Deploy from this folder using the included `Dockerfile`.
- Environment variables: set `WH_WEB_PASSWORD` (required), optional `WH_API_KEY`,
  `WH_HOST=0.0.0.0`, `WH_PORT=<the port the platform expects>`.
- Attach a persistent volume mounted at `/app/data` (and `/app/results`) or state is lost on redeploy.
- Health check path: `/health`. Give the container at least 2 GB RAM (Chromium) and `shm_size` 1 GB if supported.

## Notes
- First start has no saved page data: copy old `results/` in (see README.md) or run a WinningHunter scan.
- Secrets are NOT in this package. Enter the WinningHunter API key in Settings or via `.env`.
