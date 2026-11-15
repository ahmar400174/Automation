# Product and source benchmark notes

Reviewed public product pages on 2026-11-05. Feature descriptions are vendor claims and are used as design inspiration, not as independent proof of performance.

- **WinningHunter** positions its product around an ad library, store/brand tracking, products/trends, and research that connects ads to stores and funnels. Source: [WinningHunter](https://winninghunter.com/).
- **Minea** emphasizes searchable multi-platform ads, filters, ad/creative analysis, competitor tracking, AI insights, and product/supplier links. Source: [Minea Ad Spy](https://www.minea.com/adspy).
- **BigSpy** groups its product around ad creatives, ad pages, domain analysis, copy research, saved/followed entities, trends, and research history. Source: [BigSpy](https://bigspy.com/).

These patterns support a useful organization for this project: a fast verification queue, searchable/sortable competitor-page records, clearly separated verified/inactive/unverified states, saved run history, and direct source links. The supplied backend does not currently provide cross-platform ad creative discovery, AI analysis, store revenue estimates, or supplier sourcing; the interface should not imply that it does.

Meta’s official [Ad Library API page](https://www.facebook.com/ads/library/api/) says the API includes ads about social issues/elections/politics worldwide for seven years and ads of any type delivered to the UK or EU for one year. It states that API access requires a Facebook account and identity confirmation. This does not cover all global commercial ads and conflicts with the stated no-client-identity constraint, so this implementation should not quietly enable the API.

Meta’s [Ad Library explainer](https://www.facebook.com/government-nonprofits/blog/understanding-the-ad-library) says the public Ad Library contains active ads and explains that some displayed values (such as political-ad impressions and spend) are ranges, not exact values. The supplied verifier also parses a public-page “~N results” count. Therefore the application should call its output “Meta-reported count” and preserve the displayed approximation marker; it should not label every value an exact count.

## Throughput, persistence, and source limits

- Meta's [official Ad Library API page](https://www.facebook.com/ads/library/api/) says API coverage is worldwide for political/social-issue ads over seven years and all ad types only for the UK/EU over one year; authorization requires a Facebook account and identity confirmation. That is not a drop-in, identity-free source for globally active commercial ads.
- [BUbiNG: Massive Crawling for the Masses](https://arxiv.org/abs/1601.06919) describes a distributed Java crawler; its abstract reports thousands of pages per second per agent on sizeable hardware while respecting host- and IP-level politeness. That result is not comparable to loading Meta Ad Library pages in a small, single-machine browser, and does not imply Meta will allow this workload at that rate. It supports the systems principle of efficient scheduling under target-specific limits, not concurrency escalation. Safe gains here should prioritize de-duplication, eliminating repeated browser work, and publishing completed-country artifacts.
- The requested 100 pages/minute is not a claim we can substantiate for Meta's public website. The actual limiter is Meta's response time and throttling; measured successful pages/minute are the only honest benchmark. Never use circuit rotation, account identity, or concurrency escalation to bypass a source response.
- A country-completion checkpoint is a useful durability boundary: atomically replace the HTML/XLSX/manifest snapshot after a country workbook is complete, mark countries not yet included as partial, and retain the prior valid report if the snapshot contains no trusted Meta answer.
- Activity history should be stored with each job record and bounded per job, with ISO timestamps and links to job details; it must not rely only on browser memory or an ephemeral UI event stream.

The paper title, authors, abstract, and arXiv record were checked directly. Its high throughput depended on sizeable distributed infrastructure plus politeness constraints, so it does not establish a 100-page/minute benchmark for this Meta-browser verifier.

## Interface implications

Make the first screen answer “what is verified, what remains, when was it checked, and what source was used?” before exposing configuration. Surface the queue and recent jobs. Add meaningful filters only for fields the data already has. Show observed throughput and source-limit/cooldown status rather than vendor-style fixed speed claims. Keep unverified values blank/dash and distinguish them from a verified zero.
