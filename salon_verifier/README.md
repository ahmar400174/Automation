# Spreadsheet Verifier

A local dashboard for checking data in different CSV and Excel workbooks. It never changes the selected source file. Results are stored in `verifier.sqlite3` beside the app.

## Start

From `C:\Users\AiBit\Desktop\Excel`:

```powershell
python salon_verifier/app.py
```

Open `http://127.0.0.1:8770`. Python 3.10 or newer is needed; no package installation is required. If that port is occupied, run `python salon_verifier/app.py --port 8771` and open the matching URL.

## Choose data

- Select any `.csv`, `.xlsx`, or `.xlsm` file in the parent folder, or upload one through the dashboard. Legacy `.xls` files need conversion to `.xlsx` first.
- For Excel, choose a worksheet. The dashboard initially selects the first sheet. Set **Header row** to `0` for automatic detection, enter the row number containing column names, or use `-1` when there are no column names.
- Duplicate column names are retained with suffixes such as `(2)`.
- Formula cells use the value cached in the workbook. If a workbook has not been recalculated and saved in Excel, some formula cells may appear empty; open and save it in Excel before checking those values.
- Open **Map column meanings** to assign a role to headers the app does not recognize. Only mapped fields receive field-specific checks. Unmapped columns remain visible and are labelled as unverified by public sources.
- Selecting a different file, sheet, header row, or mapping makes old results stale. Run checks again before exporting.

## Checks

**Run available checks** checks missing values, obvious format errors, and cross-field consistency. **Check websites and fill missing data** is selected by default. It crawls the supplied company website and up to three relevant linked contact, location, team, or booking pages on the same host. It fills missing business contact or address fields when it finds one clear value, and records the source page. Owner details are filled only when the website explicitly identifies an owner or founder. Ambiguous or unfound values remain blank in the updated CSV. This may take time and some sites may block automated requests or render content only with JavaScript.

The site check uses the company website column selected under **Map column meanings**. It does not search the whole web, log in to LinkedIn, or treat a missing website mention as proof that a value is false. Owner details and negative claims may remain unconfirmed. Internal call notes, status, response, and action items require a separate call log or human confirmation. Entry dates require the original entry log or file history to confirm when a row was added.

Use **Download results CSV** for one result per source cell, with status, reason, and evidence. Results from an older source cannot be exported for a newly selected source.

Use **Download updated data CSV** for one row per source record. It keeps the original columns in order, fills missing values found on the website, leaves other missing values blank, and appends the same five columns for every file: `system_verification_status`, `system_verification_flags`, `system_verification_checks`, `system_verification_evidence`, and `system_verified_at`. The button becomes available after checks finish on the current spreadsheet; run checks again if the source or reference file changes. Existing nonblank values are not replaced.

| Status | Meaning |
| --- | --- |
| `reference_match` / `reference_conflict` | Agrees / disagrees with a uniquely matched independent reference row. |
| `public_mention` | Value appears on a crawled website page; may still be stale. |
| `website_filled` | A missing value was filled from one clear value on the company website. |
| `structured_match` / `needs_review` | Structured business data agrees / differs or a booking claim needs review. |
| `reachable` / `fetch_failed` | Website returned readable text / website request failed. |
| `consistent` / `conflict` | Cross-field value agrees / disagrees. |
| `format_valid` / `invalid` | Syntax looks plausible / fails a syntax check. |
| `present` / `missing` | Value is supplied / not supplied. |
| `internal_unverified` | Internal or unmapped field; no public confirmation. |

The old `public_findings.json` applies only to the earlier salon profiles CSV and is hidden for every other selected source.

## Data and privacy

The server binds only to `127.0.0.1`. Uploaded spreadsheets are copied to `salon_verifier/uploads/`. The dashboard sends only supplied company website requests when public checking is enabled. It does not submit full spreadsheets to an external API.
