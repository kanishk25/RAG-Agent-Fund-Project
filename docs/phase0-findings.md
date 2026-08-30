# Phase 0 — Source Discovery & Decision Spike: Findings

Executed per [implementation-plan.md](implementation-plan.md) Phase 0. Observation date: **2026-08-30**.
Fixtures: `tests/fixtures/groww/*.json` (five extracted payloads).

**Verdict: GO.** Both gating tasks pass. Four of nine in-scope facts carry publisher dates; three more are served under a page-level date by decision; two are out of corpus.

---

## P0.0 — robots.txt ✅ PASS

`https://groww.in/robots.txt` (HTTP 200) disallows for `User-agent: *`:

```
/dashboard/  /onboarding/  /pages/*  /v1/api/*  /.well-known/  /cdn-cgi/
/mutual-funds/filter?*  /mutual-funds/compare/*  /user/  /mutual-funds/user/
/stocks/filter?*  /stocks/positions/  /stocks/user/  /report/
```

**Our target paths — `/mutual-funds/<scheme-slug>` — are not disallowed.** Crawling is permitted, and PS §4.5 behaviour 8 is satisfiable.

> **`Disallow: /v1/api/*` matters more than it looks.** ARCH §15.3 listed "Groww's underlying JSON endpoints" as a fallback if the pages proved client-rendered. **That fallback is closed** — those endpoints are robots-disallowed. Fortunately P0.1 made it unnecessary.

---

## P0.1 — Rendering mode ✅ PASS (better than expected)

Pages are **server-rendered**. A plain `curl` of the large-and-midcap page returned 413 KB of HTML containing `NAV` (29×), `Expense ratio` (12×), and `Exit load` (13×).

**No Playwright needed.** ARCH §15.3's client-side-rendering risk is closed, and the ~1–2 day contingency is not spent.

### The significant finding: a typed JSON payload

Each page embeds a Next.js `__NEXT_DATA__` script whose `props.pageProps.mfServerSideData` object holds **96 typed fields** — every in-scope fact as a native value, not as text to be scraped:

```json
{ "nav": 41.7091, "nav_date": "28-Aug-2026", "expense_ratio": "0.92",
  "exit_load": "Exit load of 1%, if redeemed within 365 days.",
  "min_sip_investment": 500, "benchmark": "NIFTY Large Midcap 250 TRI",
  "lock_in": {"years": null, "months": null, "days": null},
  "holdings": [{"company_name": "Zomato Ltd", "corpus_per": 5.41,
                "portfolio_date": "2026-07-30T18:30:00.000Z"}] }
```

**This is materially better than HTML scraping** and changes two things downstream:

1. **Extraction is a JSON key lookup, not CSS-selector archaeology.** ARCH §15.7's parser-brittleness risk drops considerably — a visual redesign that would break selectors leaves the payload intact. The residual risk is a *schema* change, which is rarer and fails loudly (missing key) rather than silently (wrong element).
2. **Fact-card values are exact.** ARCH §15.1 worried that "numbers are read from retrieved text rather than looked up from typed fields." Ingestion now *does* read typed fields; only the LLM's reading of the generated card remains a risk, which single-attribute cards already mitigate.

### Parsing note — the page HTML is volatile *(corrected in P2.3)*

The script tag carries a `nonce` attribute, so the extraction regex must not assume attribute order: use `<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>`.

> **Correction.** This section originally said the nonce "changes on every request". Measured in P2.3, that is wrong: across two fetches 4 seconds apart the **nonce was unchanged** (it rotates on a slower cycle — it did differ an hour later). The element that changes on *every* response is Cloudflare's **`data-cfemail`** attribute.
>
> Measured on two fetches of the same page, 4 seconds apart:
>
> | | Result |
> |---|---|
> | Raw HTML hash | **differs** (identical 413,184-byte length) |
> | Total differing characters | **52**, all in one `data-cfemail` value |
> | `mfServerSideData` hash | **identical** — 0 differing keys |
>
> The practical conclusion is unchanged and in fact stronger: **hashing raw HTML would report "changed" on every single fetch.** Note that stripping the nonce alone would *not* have saved you — `data-cfemail` guarantees a fresh hash regardless. Hash the extracted payload fields, not the page.

---

## P0.2 / P0.3 — Fact coverage and `source_as_of` matrix

| In-scope fact (PS §4.2) | JSON path | Date source | Status |
|---|---|---|---|
| **NAV** | `nav` | `nav_date` | ✅ Publisher-dated |
| **Expense ratio** | `expense_ratio` | `historic_fund_expense[max].as_on_date` | ✅ Publisher-dated |
| **Exit load** | `exit_load` | `historic_exit_loads[max].as_on_date` | ✅ Publisher-dated |
| **Holdings** | `holdings[]` | `holdings[].portfolio_date` | ✅ Publisher-dated |
| **Minimum SIP** | `min_sip_investment` | *none* | ⚠️ Page-level date (decision below) |
| **Lock-in** | `lock_in{years,months,days}` | *none* | ⚠️ Page-level date |
| **Benchmark** | `benchmark` | *none* | ⚠️ Page-level date |
| **Riskometer** | — | — | ❌ **Out of corpus** |
| **Statement / capital-gains process** | — | — | ❌ **Out of corpus** |

### Verified values (as of 2026-08-30, `nav_date` 28-Aug-2026)

| scheme_id | Scheme | NAV | TER | Min SIP | Lock-in | Benchmark |
|---|---|---|---|---|---|---|
| `mo_large_midcap` | Large and Midcap Fund | 41.7091 | 0.92 | 500 | — | NIFTY Large Midcap 250 TRI |
| `mo_bse_value` | BSE Enhanced Value Index | 27.4719 | 0.52 | 500 | — | BSE Enhanced Value TRI |
| `mo_elss` | **ELSS Tax Saver Fund** | 67.5754 | 0.97 | 500 | **3 years** | NIFTY 500 TRI |
| `mo_next50` | Nifty Next 50 Index | 26.6778 | 0.41 | 500 | — | NIFTY Next 50 TRI |
| `mo_bse_fin` | BSE Financials ex Bank 30 | 18.3384 | 0.52 | 500 | — | BSE Financials ex Bank 30 TRI |

All five pages returned HTTP 200; holdings `portfolio_date` is 2026-07-30 across all five (~32–52 holdings each).

### ⚠️ The ELSS scheme has been renamed

The URL slug is `motilal-oswal-most-focused-long-term-fund-direct-growth`, but the payload reports **"Motilal Oswal ELSS Tax Saver Fund Direct Growth"**. The old URL still resolves.

**Consequence for P3 scheme resolution:** aliases must cover *both* names. A user asking about the "ELSS Tax Saver Fund" and one asking about the "Most Focused Long Term Fund" mean the same scheme, and neither string matches the other. This is exactly the case that would otherwise produce a confusing "scheme not found" refusal for a scheme that is in corpus.

---

## P0.4 — Out-of-corpus set

### ❌ Riskometer — excluded (contradictory *and* undated)

No current-riskometer field exists. Two candidates, and they **disagree with each other** for the same fund:

| Field | `mo_elss` value |
|---|---|
| `nfo_risk` | `"Moderately High Riskometer"` — NFO-time risk, not current |
| `return_stats[0].risk` | `"Very High"` |

Neither is dated, and the SEBI riskometer is a monthly-reviewed regulatory disclosure that this payload does not carry. Serving either would be a guess. **Riskometer queries refuse.**

### ❌ Statement / capital-gains download process — excluded

Not present on scheme pages in any form. Would require a Groww support page outside the five URLs in PS §4.1. **Refuses.**

### ⚠️ Data-quality observation — ARCH §15.2 is not theoretical

`mo_elss.category_info` describes a **"Contrarian investment strategy"** with `sub_type: "Contra"`, while `sub_category` on the same object says `"ELSS"`. Groww's own metadata is internally inconsistent for this fund.

This is a live instance of ARCH §15.2's "no cross-check is possible" limitation. It does not affect the seven ingested facts — `category_info` is not one of them — but it is direct evidence that the single-source decision carries real risk, not a hypothetical one.

### 🚫 Barred fields — the payload carries content PS §5.3 forbids

The 96-field object includes a substantial amount of material that **must never be ingested**:

| Field | Why barred |
|---|---|
| `groww_rating` (4–5 stars) | A rating — recommendation-adjacent (PS §5.3) |
| `analysis[]` | PROS/CONS text: *"Lower expense ratio"*, *"Consistently higher annualised returns than category average"* — comparative and evaluative |
| `simple_return`, `sip_return`, `return_stats` | Performance data (PS §5.3) |
| `peerComparison[]` | Cross-scheme comparison (PS §5.3) |
| `category_info.tax_impact` | Tax guidance — advisory-adjacent |
| `fund_manager`, `aum`, `portfolio_turnover` | Not in-scope; excluded for scope, not compliance |

**Ingestion must use a field allowlist, not a blocklist.** A "take the payload and chunk it" approach would put star ratings and return comparisons directly into the retrieval index — the exact content the guardrails exist to prevent, smuggled in as *retrieved context* where it would look authoritative. This is the single most important implementation constraint P0 produced.

---

## P0.5 — Crawl policy

| Setting | Value |
|---|---|
| `robots.txt` | No `Crawl-delay` directive present |
| Adopted delay | **3 seconds** between page fetches (used during this spike) |
| Requests per run | **5** — one per scheme page; every fact comes from one fetch |
| User-Agent | Identifying string, e.g. `mf-faq-bot/1.0 (+<repo-url>)` |
| Re-check | `robots.txt` fetched and evaluated **every run** (edge-case I-08) |

Five requests per day against a commercial site is negligible load.

---

## P0.6 — Refusal links ✅ CLOSED

Registry: [`config/refusal_links.yaml`](../config/refusal_links.yaml). All URLs verified HTTP 200 on 2026-08-30. `motilaloswalmf.com/robots.txt` is `Allow: /` for all agents (and explicitly welcomes AI crawlers), so daily HEAD link-health checks are permitted.

### The link depends on *why* we refused

Two requirements are in play, and they want different destinations:

| Requirement | Refusal reason | Link |
|---|---|---|
| **PS §5.3** — performance queries get "a link to the official factsheet only" | `performance_barred`, `stale_content` | **AMC scheme page** |
| **PS §4.3** — advisory refusals get "a relevant educational link" | advisory, mixed, injection, out-of-corpus, barred-field | **Investor education page** |
| — | `ambiguous_scheme`, `pii` | **No outbound link** |

**Why the split, rather than one link everywhere:** the AMC scheme pages are marketing pages. One is titled *"Invest In Motilal Oswal Focused Fund: NAV And Returns"*. Returning that in response to *"Should I invest?"* would refuse to advise and then route the user to a page encouraging investment while showing returns — inverting the refusal's entire purpose. Eval case `R-route-advisory-not-product` guards this.

Conversely, those same pages are exactly right for performance queries, which is precisely what PS §5.3 asks for.

### Scheme page URLs (corrected)

Two URLs in the original proposal pointed at funds outside the corpus — `motilal-oswal-quality-fund` ("Quality Fund") and `motilal-oswal-focused-fund` ("Focused Fund"). Neither is one of the five schemes; the ELSS fund in particular is *"ELSS Tax Saver Fund"*, not *"Focused Fund"*. Corrected from the AMC sitemap, and all five now use a consistent `/mutual-funds/` path:

| scheme_id | AMC scheme page |
|---|---|
| `mo_large_midcap` | `/mutual-funds/motilal-oswal-large-and-midcap-fund` |
| `mo_bse_value` | `/mutual-funds/motilal-oswal-bse-enhanced-value-index-fund` |
| `mo_elss` | `/mutual-funds/motilal-oswal-elss-tax-saver-fund` |
| `mo_next50` | `/mutual-funds/motilal-oswal-nifty-next-50-index-fund` |
| `mo_bse_fin` | `/mutual-funds/motilal-oswal-bse-financials-ex-bank-30-index-fund` |

Educational: `/investor-education` and `/investor-education/glossary`.

### ⚠️ Open improvement — educational-link neutrality

The educational pages are published by the **same AMC whose funds we cover**, so they are not a neutral third party. PS §4.3 names AMFI and SEBI as its examples, and those would be stronger for advisory refusals.

Both were unreachable from this environment (`000` in <0.2 s while `groww.in` returned 200 — a network restriction, not evidence they are down). Candidates recorded in the registry for verification: `amfiindia.com/investor-corner`, `investor.sebi.gov.in`, `sebi.gov.in/investors.html`.

**Not blocking** — refusals now have working links — but swapping AMFI/SEBI into `educational_links.default` is a one-line config change once verified, and worth doing.

### Allowlist consequence

`www.motilaloswalmf.com` joins the **link-health allowlist only** (ARCH §12). It is HEAD-checked daily and never fetched for facts, parsed, chunked, or cited. The fact allowlist remains `groww.in` alone.

---

## P0.9 — Decisions closed

### PS §8.2 — Holdings: verbatim disclosure only ✅

Serve `company_name` + `corpus_per` + `portfolio_date`, verbatim, with no derived commentary, no sector aggregation, and no "largest holding is…" phrasing beyond what the data literally states.

**Chunking (resolves edge-case R-08):** holdings are **one combined chunk per scheme**, not one chunk per holding. With 32–52 holdings and `top_k=4`, per-holding chunks would silently truncate a "top holdings" answer into a confident, well-formatted, *incomplete* list. Given the ≤3-sentence limit, answers will cite the top few by `corpus_per` and state the portfolio date.

### PS §8.6 — Behaviour past `max_age` ✅

| `doc_type` | `max_age` | Past it |
|---|---|---|
| `nav` | 1 business day | **Refuse** + state the last known declaration date |
| `expense_ratio`, `holdings` | 45 days | **Flag** — answer with a visible outdated warning |
| `exit_load` | 400 days | **Flag** |
| `min_sip`, `lock_in`, `benchmark` | *exempt* | Never gated (see the page-level-date decision) |

NAV takes the strict path per ARCH §15.4 reasoning; everything else flags, preserving usefulness.

### PS §8.7 — NAV wording on non-business days ✅

Always state the declaration date; never imply liveness. Fixed template:

> *"The NAV of {scheme} was ₹{value}, as declared on {nav_date}."*

Never "the current NAV is" or "today's NAV". This wording is correct on a Sunday, a market holiday, and a Tuesday alike — which is why it is fixed rather than conditional. No market-holiday calendar is needed for *wording* (edge-case F-01/F-02); one is still needed for the NAV **staleness alert** (ARCH §8.5).

### New — undated facts use the page-level `nav_date` ✅ *(stakeholder decision)*

`min_sip`, `lock_in`, and `benchmark` are ingested and served, taking **`nav_date` as their `source_as_of`**.

**Recorded consequences** — this was chosen over the two alternatives with these costs accepted:

1. **The footer date is not these facts' publication date.** A benchmark index unchanged since the fund's 2015 launch will carry a footer reading `Last updated from sources: 28-Aug-2026`. That date is when the *page* was current, not when the benchmark was set.
2. **They are exempt from the freshness gate.** Since `nav_date` advances daily, `max_age` can never trip for them — so PS §4.5's staleness protection does not apply to these three facts. If Groww silently changed a benchmark, nothing would flag it.
3. **Implementation requirement — keep the date out of the card text.** The fact card body must contain only the value (`"Minimum SIP amount: ₹500"`), with `source_as_of` in *metadata only*. If the date were rendered into the card, the text would change daily, the content hash would change daily, and all three facts would re-embed every run — reintroducing the exact waste ARCH §8.3 is designed to prevent.

---

## Exit Criteria

- [x] `groww.in/robots.txt` confirmed to permit the target paths
- [x] Rendering mode determined — server-rendered; `httpx` + JSON extraction, no Playwright
- [x] Every in-scope fact confirmed present and dated, or on the out-of-corpus list
- [x] Coverage gaps accepted by the stakeholder (riskometer, statement process; undated-facts decision)
- [x] Golden + refusal sets committed (`eval/golden.yaml`, `eval/refusal.yaml`, `eval/ambiguity.yaml`)
- [x] The three open questions decided and written down
- [x] **Refusal links registered and verified** (`config/refusal_links.yaml`) — routed by refusal reason

**Phase 0 is complete.** One non-blocking improvement carried forward: prefer AMFI/SEBI over AMC-published educational pages once reachable (P0.6).

---

## Carried into later phases

| Finding | Affects | Action |
|---|---|---|
| Field allowlist required — payload carries ratings, returns, PROS/CONS | **P2.4** | Ingest only the 7 approved fields; never chunk the payload wholesale |
| `nonce` changes per request | **P2.3** | Hash extracted fields, not raw HTML |
| ELSS fund renamed; slug ≠ display name | **P3.5** | Alias both names to `mo_elss` |
| Holdings = one chunk per scheme | **P3.1** | Prevents `top_k` truncation |
| Undated facts: date in metadata, not card text | **P2.6** | Keeps hashes stable |
| `/v1/api/*` disallowed | **P2.1** | JSON endpoints are not a permitted fallback |
| Groww metadata self-inconsistency (ELSS/Contra) | **ARCH §15.2** | Evidence the no-cross-check risk is real |
| Extraction regex must tolerate attribute order | **P2.4** | `<script id="__NEXT_DATA__"[^>]*>` |

---

_Facts-only. No investment advice._
