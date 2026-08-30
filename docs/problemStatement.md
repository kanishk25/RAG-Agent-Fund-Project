# Problem Statement: Mutual Fund FAQ Assistant (Facts-Only Q&A)

> **Disclaimer snippet (must appear in the product UI):**
> _"Facts-only. No investment advice."_

---

## 1. Overview

Build a **facts-only FAQ assistant** for mutual fund schemes, using **Groww** as the reference product context.

The assistant answers **objective, verifiable** queries about mutual funds by retrieving information **exclusively from official public sources** — AMC (Asset Management Company) websites, **AMFI**, and **SEBI**.

The system must **strictly avoid** investment advice, opinions, or recommendations. Every response carries a **single, clear source link** and adheres to defined constraints around clarity, accuracy, and compliance.

**Guiding principle: accuracy over intelligence.** A correct refusal is a better outcome than a confident-but-unsourced answer.

---

## 2. Objective

Design and implement a **lightweight Retrieval-Augmented Generation (RAG)** assistant that:

1. Answers factual queries about mutual fund schemes
2. Uses a **curated corpus** of official documents
3. Provides **concise, source-backed** responses

---

## 3. Target Users

| User | Need |
|---|---|
| Retail investors | Comparing mutual fund schemes on factual attributes |
| Customer support & content teams | Handling repetitive, high-volume mutual fund queries |

---

## 4. Scope of Work

### 4.1 Corpus Definition

- Select **one AMC**
- Choose **3–5 mutual fund schemes**, ensuring **category diversity** (e.g., large-cap, flexi-cap, ELSS)

**Selected AMC:** Motilal Oswal Asset Management

**Reference scheme URLs (Groww product context):**

1. https://groww.in/mutual-funds/motilal-oswal-large-and-midcap-fund-direct-growth
2. https://groww.in/mutual-funds/motilal-oswal-bse-enhanced-value-index-fund-direct-growth
3. https://groww.in/mutual-funds/motilal-oswal-most-focused-long-term-fund-direct-growth _(ELSS)_
4. https://groww.in/mutual-funds/motilal-oswal-nifty-next-50-index-fund-direct-growth
5. https://groww.in/mutual-funds/motilal-oswal-bse-financials-ex-bank-30-index-fund-direct-growth

This set spans **large & midcap, factor/value index, ELSS, broad-market index, and sectoral index** categories — satisfying the diversity requirement.

### 4.2 FAQ Assistant Requirements

**In-scope query types (facts-only):**

- **Current NAV (Net Asset Value) of a scheme**, with its declaration date
- Expense ratio of a scheme
- Exit load details
- Minimum SIP amount
- ELSS lock-in period
- Riskometer classification
- Benchmark index
- Holdings / holdings analysis
- Process to download statements or capital gains reports

> **NAV boundary.** The **latest declared NAV** is a disclosed, verifiable fact and is in scope. What remains out of scope per §5.3 is anything *derived* from NAV — historical NAV series, returns, CAGR, gain/loss calculations, or NAV comparisons between schemes. State the NAV and its date; do not compute with it.

**Hard response format rules — every answer must:**

| Rule | Constraint |
|---|---|
| Length | **Maximum 3 sentences** |
| Citation | **Exactly one** citation link |
| Footer | `Last updated from sources: <date>` |

### 4.3 Refusal Handling

The assistant **must refuse** non-factual or advisory queries, e.g.:

- "Should I invest in this fund?"
- "Which fund is better?"

**A refusal response must:**

1. Be polite and clearly worded
2. Reinforce the facts-only limitation
3. Provide a relevant **educational link** (e.g., AMFI or SEBI resource)

### 4.4 User Interface (Minimal)

The interface must include:

- A **welcome message**
- **Three example questions**
- A **visible disclaimer**: _"Facts-only. No investment advice."_

### 4.5 Scheduled Corpus Refresh (Scheduler)

The corpus is **not** a one-time ingestion. A scheduler must re-fetch the official sources on an **agreed frequency** and update the retrieval index.

**Why this is a hard requirement, not a nice-to-have:** every answer ends with `Last updated from sources: <date>`. If the corpus goes stale, that footer becomes a false claim — a compliance failure, not just a data-quality one. Expense ratios, exit loads, and holdings all change; a confidently-cited stale expense ratio is worse than a refusal.

#### Refresh cadence: one daily run for the entire corpus

**Decision:** the scheduler runs **once per day and refreshes every source**, regardless of how often that source actually changes. No per-document-type intervals.

**Rationale.** Change detection (behaviour 1 below) makes an over-frequent crawl nearly free: an unchanged document costs one HTTP fetch and a hash comparison, and the expensive step — re-chunking and re-embedding — only fires when content actually changed. Against that, per-type cadences cost real complexity: multiple schedules to configure, several failure surfaces to alert on, and interleaved partial updates that make "what was fresh when?" hard to answer. One run per day means **one schedule, one run log, one alert, one atomic index swap.** Cadence tuning can come later if crawl volume ever justifies it.

The change frequencies below are therefore **informational, not fetch schedules**. They matter for two things: setting `max_age` staleness thresholds, and knowing when a *missing* update signals an upstream problem worth alerting on.

| Content | How often it actually changes | Expect a new `source_as_of` |
|---|---|---|
| **NAV (latest declared)** | **Every business day** — AMCs upload to AMFI by 11 PM | Each business day |
| Monthly portfolio / holdings disclosure | Monthly (AMCs publish by ~10th of following month) | Within ~40 days |
| Expense ratio (TER) | On change; AMCs disclose current TER continuously | No fixed cycle |
| Riskometer classification | Monthly review per SEBI | Within ~40 days |
| Exit load, minimum SIP, lock-in | On addendum only (infrequent) | No fixed cycle |
| SID / KIM / SAI | On addendum / annual refresh | Within ~1 year |
| Benchmark index | Rare | No fixed cycle |
| Statement & capital-gains process pages | Rare | No fixed cycle |
| AMFI / SEBI educational links (refusal responses) | Rare | Link-health check only |

**Run timing.** The daily run should fire **after AMFI's NAV publication window (~11 PM IST)**. NAV is the only source with a hard daily deadline, so it sets the schedule for the whole run — a run timed earlier would serve the previous day's NAV every single day.

> **NAV is the tightest constraint in the system.** It is the only daily-changing fact in scope, and it is stale within one business day — so it needs its own short `max_age` (roughly one business day) rather than the corpus-wide default. A NAV served without its declaration date, or carrying a stale date, is a factual error even though nothing was computed.
>
> **Canonical source:** per the §8.1 decision, NAV is read from the **Groww scheme page**, which displays the latest declared NAV with its declaration date. There is no AMFI fallback and no cross-check. If a page omits the declaration date or the fetch fails, NAV goes stale and the 1-business-day `max_age` causes the assistant to **refuse** — which is the correct fail-closed behaviour, but it means NAV availability rests entirely on that one page.
>
> **Still excluded:** historical NAV series and derived returns. Those stay out of the corpus entirely — the assistant links to the official factsheet, per §5.3.

#### Scheduler behaviour

1. **Change detection first.** Fetch, hash the normalised content, and compare against the stored hash. Re-chunk and re-embed **only** changed documents — cheap crawls, expensive embedding only when warranted.
2. **Two timestamps per document, tracked separately:**
   - `fetched_at` — when the scheduler last retrieved it
   - `source_as_of` — the as-of / publication date printed *in* the document
   The answer footer uses **`source_as_of`** (see §8.3).
3. **Atomic index swap.** A failed or partial run must never leave the index half-updated; the previous good index keeps serving.
4. **Fail loud, never silently stale.** If a source is unreachable or a run fails, alert and retain the last good copy — do not drop the document and do not silently serve it as fresh.
5. **Staleness policy.** Each document type carries a `max_age`, measured against **`source_as_of` — never `fetched_at`**. A daily run does not make a document fresh; it only proves the upstream copy has not changed. Past `max_age`, the assistant must **flag the answer as potentially outdated or refuse and link to the live source** rather than answer from a known-stale chunk.
6. **Alert on missing updates.** Because every source is fetched daily, a document that *should* have changed but hasn't is a detectable signal — no new NAV on a business day, or holdings still showing last quarter past the ~40-day mark. Treat that as an upstream problem worth alerting on, using the "expect a new `source_as_of`" column above.
7. **Manual trigger + backfill.** Operators can force a refresh of one scheme or the whole corpus without waiting for the next daily tick (needed after an addendum).
8. **Polite crawling.** Respect `robots.txt`, rate-limit requests, and identify the agent — these are regulated public sites, not scrape targets. A daily full-corpus crawl makes this non-negotiable: the same small set of official pages is hit every single day.
9. **Run log / observability.** Persist per-run: sources attempted, changed, failed, and last successful run per source. This log is the evidence for the freshness claim.

---

## 5. Constraints

### 5.1 Data and Sources

> **AMENDED — see §8.1.** The original brief said "use only official public sources" while simultaneously defining the corpus as five Groww URLs (§4.1). That contradiction has been resolved **in favour of Groww**, and this section is amended to match what is actually being built.

- **Sole corpus and citation source: the Groww scheme pages listed in §4.1.** Every fact the assistant states, and every citation it emits, comes from those five pages. There is **no fallback to AMC, AMFI, or SEBI for facts.**
- **Do not** fetch facts from any other domain — including AMC, AMFI, and SEBI.
- **One carve-out, required by §4.3:** refusal responses must carry an AMFI or SEBI **educational link**. These are outbound links shown to users, never a source of facts, and are subject only to a periodic liveness check (§4.5).

**Consequences, accepted deliberately:**

1. **Citations are secondary-source.** Groww is a distributor republishing AMC data. The system must never describe its citations as "official AMC sources."
2. **No cross-check exists.** With a single source there is nothing to verify against, so a Groww transcription error propagates into answers undetected.
3. **Coverage is bounded by Groww.** Any in-scope fact that Groww does not display, or displays without an as-of date, is **out of corpus** — the assistant refuses it. It is not sourced from elsewhere.
4. **Single point of failure.** If Groww blocks crawling or restructures its pages, the corpus has no alternative supply. See the P0 gates in the implementation plan.

### 5.2 Privacy and Security
Do **not** collect, store, or process:
- PAN or Aadhaar numbers
- Account numbers
- OTPs
- Email addresses or phone numbers

### 5.3 Content Restrictions
- **No** investment advice or recommendations
- **No** performance comparisons or return calculations
- For performance-related queries → **link to the official factsheet only**

### 5.4 Transparency
- Responses must be short, factual, and verifiable
- Every answer must include a **source link** and a **last-updated date**

---

## 6. Expected Deliverables

1. **README document**, covering:
   - Setup instructions
   - Selected AMC and schemes
   - Architecture overview (RAG approach)
   - **Scheduler design: cadence table, how to run it, how to trigger a manual refresh**
   - Known limitations
2. **Disclaimer snippet**: _"Facts-only. No investment advice."_
3. The working assistant (corpus + retrieval + generation + minimal UI)
4. **The refresh scheduler**, including its run log and staleness configuration

---

## 7. Success Criteria

- [ ] Accurate retrieval of factual mutual fund information
- [ ] Strict adherence to facts-only responses
- [ ] Consistent inclusion of valid source citations
- [ ] Proper refusal of advisory queries
- [ ] Clean, minimal, and user-friendly interface
- [ ] **A single daily scheduler run refreshes the whole corpus and updates the index without manual intervention**
- [ ] **The `Last updated from sources` date provably reflects the source document, and stale content is flagged or refused rather than served as current**

---

## 8. Open Questions / Tensions to Resolve

These are ambiguities in the source brief that need an explicit decision during implementation:

1. ~~**Groww vs. "official sources only".**~~ **DECIDED: Groww is the sole corpus and citation source — no AMC/AMFI/SEBI fallback for facts.** The brief named Groww as the reference product context and supplied no scheme list other than Groww URLs, so those pages are used directly: they carry NAV, expense ratio, exit load, minimum SIP, riskometer, benchmark, and holdings in one stable, dated layout. §5.1 has been amended accordingly, including the four accepted consequences listed there. AMFI/SEBI appear only as educational links in refusals (§4.3). **Two dependencies to verify in P0, before any code:** `groww.in/robots.txt` must permit crawling `/mutual-funds/` (§4.5 behaviour 8 makes this non-negotiable), and the pages must expose facts in server-rendered HTML. With no fallback corpus, either failing is **project-blocking rather than merely costly** — it would require reopening this decision with the stakeholder, not switching to a backup plan.
2. ~~**"Holdings analysis" vs. "no analysis".**~~ **DECIDED (P0.9): verbatim disclosure only.** Serve `company_name` + `corpus_per` + `portfolio_date` exactly as published, with no sector aggregation and no derived commentary. **Chunking:** holdings are one combined chunk per scheme — with 32–52 holdings and `top_k=4`, per-holding chunks would truncate a "top holdings" answer into a confident but incomplete list. See [phase0-findings.md](phase0-findings.md).
3. **`<date>` semantics.** The footer date should be the **as-of date of the source document** (`source_as_of`), not the query date and not the scheduler's `fetched_at` — this is what makes the answer verifiable. A document re-fetched today with no upstream change still carries its original as-of date.
4. **Multi-scheme questions.** "Which of these is ELSS?" is factual, but sits close to comparison. Likely resolution: allowed for **objective attributes**, refused for anything implying a ranking or preference.
5. ~~**Refresh cadence sign-off.**~~ **DECIDED:** a single daily run refreshes the whole corpus, timed after the ~11 PM IST AMFI NAV window. Per-type cadences were rejected as premature complexity — change detection makes redundant fetches cheap, and one schedule means one failure surface. Revisit only if crawl volume or source rate-limits make it necessary. See §4.5.
6. ~~**Behaviour past `max_age`.**~~ **DECIDED (P0.9): NAV refuses, everything else flags.** `nav` (1 business day) → refuse, stating the last known declaration date. `expense_ratio` / `holdings` (45 days) and `exit_load` (400 days) → answer with a visible outdated warning. `min_sip` / `lock_in` / `benchmark` are **exempt from the gate** — see item 9.
7. ~~**NAV on non-business days.**~~ **DECIDED (P0.9): a single fixed template**, correct on every day of the week: *"The NAV of {scheme} was ₹{value}, as declared on {nav_date}."* Never "current NAV" or "today's NAV". Because the wording is unconditional, no market-holiday calendar is needed for phrasing — one is still required for the NAV staleness *alert* (architecture §8.5).
9. **NEW (P0.9) — undated facts use the page-level `nav_date`.** `min_sip`, `lock_in`, and `benchmark` appear on Groww with no as-of date of their own. **DECIDED:** ingest and serve them, taking `nav_date` as their `source_as_of`. Three consequences accepted: (a) the footer date is the page's currency date, **not** these facts' publication date — a benchmark unchanged since 2015 will carry today's date; (b) because `nav_date` advances daily, `max_age` can never trip, so these three facts have **no staleness protection**; (c) implementation must keep the date in metadata only, never in the fact-card text, or all three would re-embed daily and defeat change detection. This is a documented departure from §8.3's principle, chosen to preserve three in-scope query types (§4.2) rather than refuse them.
10. **NEW (P0.9) — riskometer and statement/capital-gains process are out of corpus.** Riskometer has no reliable dated field (`nfo_risk` and `return_stats[].risk` disagree with each other and neither is dated); the statement-download process is absent from scheme pages entirely. Both refuse. See [phase0-findings.md](phase0-findings.md) §P0.4.

8. ~~**Where the scheduler runs.**~~ **DECIDED: a scheduled GitHub Actions workflow** (`0 18 * * *` UTC = 23:30 IST) runs the full ingestion pipeline daily — scrape, normalise, chunk, embed — and **commits the rebuilt ChromaDB index back to the repository**. The commit is the atomic publication step, git history is the audit trail, and `git revert` is the rollback. Refresh no longer depends on API uptime, and the serving process becomes read-only. Manual refresh is `workflow_dispatch`. **Watch item:** Actions disables scheduled workflows after 60 days of repo inactivity — a *silent* stop, which needs external monitoring (see architecture §15.6).

---

## 9. Summary

Build a **trustworthy, transparent, and compliant** mutual fund FAQ assistant that **prioritizes accuracy over intelligence**. Users should receive only **verified, source-backed** financial information — with no advisory bias and no speculative content.

---

_Source: [problemStatement.txt](problemStatement.txt)_
