# RMD Engineering College – Phase 1 structural crawl

Archival/discovery crawl of `https://www.rmd.ac.in/` (HTML only). Original page
source is preserved byte-for-byte; assets (PDF, images, …) are **recorded, never
downloaded**. Phase 2 is a separate step and is not started by this tool.

## Requirements
Python 3.8+ — standard library only (no `pip install`).

## Usage (run from this folder)
```
python crawl.py run                  # crawl; resumes automatically if interrupted
python crawl.py run --check-assets   # also HEAD-check internal assets afterwards (no bodies downloaded)
python crawl.py check-assets         # only the asset HEAD checks
python crawl.py run --retry-failed   # re-queue timeouts / 5xx failures and continue
python crawl.py export               # regenerate CSVs + reports from the database
python crawl.py status               # queue counts
```
Options: `--max-pages 10000` `--max-depth 20` `--delay 1.0` `--timeout 30`
`--retries 2` `--max-consecutive-failures 25` `--insecure`.

Ctrl+C is safe: all state lives in `data/crawl.db` (SQLite); re-run the same
command to continue. Raising `--max-pages` / `--max-depth` and re-running
continues where the safety limit stopped it. Delete `data/crawl.db` to start over.

## Layout
| Path | Contents |
|---|---|
| `data/pages.csv` | one row per HTML URL: canonical/original/final URL, status, title, depth, parent, content type/length, timestamp, link & asset counts, crawl status, saved file, SHA-256, meta description, h1 |
| `data/links.csv` | every link occurrence: source, destination, internal/external, link type, destination kind (PAGE/ASSET), status if known, anchor text, nav menu path |
| `data/external-links.csv` | external destinations (recorded, never followed) |
| `data/assets.csv` | every referenced asset: URL, source page, type, extension, filename, scope, content-type, downloadable |
| `data/errors.csv` | HTTP/network errors, robots blocks, skipped-suspicious URLs |
| `data/redirects.csv` | original → chain → final URL with status codes (incl. http/https/www variants) |
| `data/crawl.db` | resume state (SQLite) — can be queried directly |
| `html/` | original HTML, mirroring URL paths (`/a/b.html` → `html/a/b.html`, `/dir/` → `html/dir/index.html`; query-string pages get a `__q-<hash>` suffix; case-colliding/over-long paths get a deterministic hash name) |
| `reports/crawl-summary.json` | the 15-point report + exhaustion verdict |
| `reports/site-tree.txt` | (A) navigation tree from the homepage menu, (B) URL-path tree, (C) discovery tree — all generated from crawl data |
| `reports/broken-links.txt` | broken internal destinations with every referencing page |
| `PHASE-2-ASSETS.csv` | internal, downloadable assets (not confirmed broken) to fetch in Phase 2 |

## Behaviour
- **Internal hosts**: `http(s)://[www.]rmd.ac.in` → normalized to `https://www.rmd.ac.in`.
  Fragments dropped, duplicate slashes collapsed, `index.html`/`index.php`/… folded into `/`,
  tracking params (`utm_*`, `fbclid`, `gclid`, session ids…) stripped, remaining query params sorted and kept.
- **What is crawled**: any internal URL that isn't a known asset extension; Content-Type decides
  at fetch time (non-HTML responses are logged as assets, not saved as pages).
- **Links followed**: `a`, `area`, `iframe`/`frame`, meta-refresh, `onclick` URLs, `data-href`, canonical/alternate.
  Resources (img, script, css, video, `url(...)` in CSS…) go to the asset inventory.
- **Safety**: ≈1 s delay (auto back-off on 429/503), timeout, retries with back-off, `robots.txt` honoured
  (`robots.txt` + `Crawl-delay`; a missing file = no restrictions), `max-pages`, `max-depth`, URL length /
  repeated-segment / query-variant trap detection, 25-consecutive-network-failure circuit breaker.
  The stop reason is always recorded in `crawl-summary.json`.
- **Exhaustion check**: `crawl_exhausted` is `true` only if the stop reason is `QUEUE_EXHAUSTED` and
  no URLs remain pending or depth-skipped.
- External links are not checked, so "broken links" covers internal destinations only.
- Nothing is rewritten: HTML is saved exactly as received.
