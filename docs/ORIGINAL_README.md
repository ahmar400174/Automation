# WinningHunter weekly competitor scanner

Scans the WinningHunter API per country, verifies every candidate page on the Meta Ad Library, and
publishes one workbook plus one self-contained HTML dashboard per period.

## What a report contains

* **Verified active pages** — the count shown is Meta's live count for that page in the scanned country
  (`Verified active ads`). It is never the WinningHunter claim and never a sum of date-range ads.
* **Verified inactive pages** — Meta authoritatively showed 0 running ads. The row is **kept** and
  published with `Active Ads = 0`, so inactive advertisers stay visible. A zero is the most dangerous
  answer (it hides a live advertiser), so it needs more proof than a positive count: the empty state must
  stay unchanged for `WH_ZERO_SETTLE` seconds (default 6) **and** the same page opened with *all ads* must
  show that Meta knows the page and lists no Active ad. Anything less is `unverified`, never `0`.
  Zeros written by older versions carry no `confirmed` flag and are treated as `unverified` until they are
  checked again (`python verify_active.py --purge-legacy-zeros` removes them from the cache; the workflow
  does it automatically). A zero also expires after the freshness window like every other answer.
* **Unverified / stale / verification_failed pages** — Meta could not be checked for them (rate limit, never
  asked, answer too old). They are **still listed as competitors**, with their status and a dash in
  `Active Ads`: never a made-up number and never a fake `0`. The header counts them (`Unverified pages`, ...)
  and shows a `PARTIAL VERIFICATION` badge. A run in which not a single page got a verified Meta answer does
  not replace the last valid report.
* Generic platform links (facebook.com, instagram.com, linktr.ee, marketplaces, …) and empty site links
  are never competitor rows.

The Ads Library link in every row is the exact URL the verifier opened, so clicking it shows the number
that was counted. When pages were excluded because Meta could not verify them, the header shows a
`PARTIAL VERIFICATION` badge and the manifest carries `"verification": "partial"` (the run outcome stays
`complete` when every country was fully scanned).

The dashboard has three columns (`Ads Library | Site | Active Ads`) and sorts by name, score or active
count; sorting Low → High puts the genuine zero-count inactive rows first. Every row's status and
verification time are kept in the workbook, and every published count can be traced back to its source.

## Runs are isolated

Each execution gets a unique execution id and writes only:

```
results/competitors_week_<monday>.html
results/competitors_week_<monday>.xlsx
results/run_manifest.json              (this run, machine readable)
results/run_manifest_<monday>.json     (same data, kept next to the period report)
```

`work/` is purged at the start of every run. Historical weeks remain in `results/` for auditability but
are never merged into a new report and never uploaded with a new artifact. A run that publishes nothing
leaves the previous report untouched and advertises no `html_path` / `workbook_path` in its manifest.

## GitHub Actions

`.github/workflows/weekly.yml` runs on `workflow_dispatch` with three modes:

* `estimate` — one API call per country, prints the credits a scan needs; publishes no report.
* `scan` — full scan, verification, merge, dashboard and workbook.
* `rebuild` — re-exports the workbooks and dashboard from the saved scan data (`results/scan_data`), no
  API credit spent and **no Meta check** (pages without a fresh cached answer stay unverified).
* `verify` — like `rebuild`, but every page without a fresh answer is checked live on the Meta Ad Library
  first (no API credit spent). This is the mode that fills the missing `Active Ads` numbers. When Meta
  throttles the run it cools down and retries (`WH_RATE_PAUSES`, `WH_RATE_PAUSE_SECONDS`).

The artifact is collected by `python run_weekly.py --collect-artifact run_artifact`, which copies exactly
the files advertised by this run's manifest (HTML, workbook, period manifest). Cached scan data,
temporary workbooks and older weeks are never part of it. The workflow also commits `results/` back to
the branch, so the repository keeps the latest published report.

Ad pictures are embedded only for the first rows of each tab (`MAX_IMG_TOP` / `MAX_IMG_OTHERS`); every
other row keeps its remote preview URL. That keeps the artifact small without losing the preview link.

## Tests

```
python -m pytest tests -q
```

The suite is offline and deterministic: mocked API payloads, fixture workbooks and a fixture verification
cache. It covers URL scope, deduplication, active-status validation, fresh artifacts, HTML/Excel
consistency, artifact collection and the estimate/scan modes.


## Meta rate limit: the proxy (recommended for big scans)

Meta throttles the datacenter IP of GitHub Actions after a few hundred checks. The real fix is a paid
**rotating residential proxy** (pay per GB, HTTP gateway with user/password):

1. Repository -> Settings -> Secrets and variables -> Actions -> New secret `WH_PROXY`
   with the value `http://USER:PASSWORD@HOST:PORT`.
   If your provider pins an IP to a session id, write it as `http://USER-session-{session}:PASSWORD@HOST:PORT`
   (`{session}` is replaced by a new random id for every check).
2. Run the workflow as usual. The log then says `Meta checks through a proxy ... new IP for every check`,
   and the run uses 8 tabs with almost no pause. Pictures/fonts are not downloaded (saves proxy GB).

Without the proxy the run still works (slower, cool-downs, automatic rounds), but on a very big scan some
pages can stay unverified. The official Meta Ad Library API is no replacement: it only archives
commercial ads delivered to the EU/UK (no US) and needs an identity-verified token.

## Automatic rounds (one click, the run finishes the verification by itself)

Meta blocks the IP of a GitHub runner after a few hundred checks, and waiting does not unblock it for hours. Every
new workflow run starts on a new machine (a new IP). So when a run ends with pages still unverified, it starts the
next run by itself (`mode verify`: no scan, no API credit, same dates and countries). The new run asks Meta only
about the pages that are still missing. This repeats until everything is verified or a safety limit is reached:

* 8 hours since the first run of the chain (`WH_CHAIN_MAX_HOURS`)
* 15 automatic rounds (`WH_CHAIN_MAX_ROUNDS`)
* 2 rounds in a row without a single new verified page
* cancelling a run stops the chain; a run that produced no report never continues

Without a proxy a run stops asking Meta as soon as it is blocked (no waiting), and the rest of that run skips Meta.
With the `WH_PROXY` secret every check uses its own IP, a throttled answer is retried at once on another IP, and
usually no extra round is needed. Each round regenerates the full report; the last one is the final report.

Rebuild / verify runs are capped at 45 minutes of Meta checking (about one hour in total); a scan keeps
320 minutes of the 6 hour job and leaves the rest for saving and for starting the next round.
