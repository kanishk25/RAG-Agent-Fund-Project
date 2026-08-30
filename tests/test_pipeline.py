"""Full-run orchestration tests (P2.8).

Everything here drives the real pipeline — fetch, normalise, parse, render,
compare, write — over `httpx.MockTransport` serving the Phase 0 fixtures. No
network, per ARCH §15.7.

Four P2 exit criteria are proved here: all 5 schemes ingest with distinct
`source_as_of` and `fetched_at`; a fact with no extractable date is rejected
with a clear log line; the out-of-corpus facts never reach the registry; and one
page is fetched once per run despite serving seven `doc_types`.

The load-bearing test is `test_a_failed_scheme_writes_nothing_at_all`. It is the
local half of ARCH §8.4 — a failed run must leave the previous index standing,
and it must do so identically on a laptop and in Actions.
"""

from __future__ import annotations

import contextlib
import copy
import logging

import pytest

from mf_faq import db
from mf_faq.ingest.pipeline import format_report, run
from mf_faq.settings import get_sources
from tests.conftest import groww_payload as payload

SOURCES = get_sources()
SCHEME_IDS = [s.scheme_id for s in SOURCES.schemes]


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "registry.db"


@pytest.fixture
def ingest(db_path, groww_fetcher):
    """Run the real pipeline against the offline harness."""

    def _ingest(_db_path=None, scheme_ids=None, **kwargs):
        return run(
            scheme_ids,
            db_path=_db_path or db_path,
            sources=SOURCES,
            fetcher=groww_fetcher(**kwargs),
        )

    return _ingest


def documents(db_path) -> dict[str, dict]:
    with contextlib.closing(db.connect(db_path)) as conn:
        return {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM documents")}


def runs(db_path) -> list[dict]:
    with contextlib.closing(db.connect(db_path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at")]


# -- exit criterion: all 5 schemes ingest ----------------------------------


def test_all_five_schemes_ingest_from_an_empty_registry(ingest, db_path):
    report = ingest(db_path)

    assert report.status == "success"
    assert report.exit_code == 0
    assert report.committed
    assert [s.scheme_id for s in report.schemes] == SCHEME_IDS
    assert all(s.ok for s in report.schemes)

    stored = documents(db_path)
    assert len(stored) == 35  # 5 schemes x 7 facts
    assert report.summary.new == 35
    assert {r["scheme_id"] for r in stored.values()} == set(SCHEME_IDS)
    assert all(r["status"] == "ok" for r in stored.values())


def test_documents_carry_distinct_source_as_of_and_fetched_at(ingest, db_path):
    """P2 exit criterion, and the PS §8.3 distinction the schema turns on.

    `source_as_of` is the date printed in the source and varies by fact;
    `fetched_at` is when we looked and is one value for the whole run. They must
    both be populated and must never be the same thing.
    """
    ingest(db_path)
    stored = documents(db_path)

    as_of = {r["source_as_of"] for r in stored.values()}
    fetched = {r["fetched_at"] for r in stored.values()}

    assert len(as_of) > 1  # holdings is month-end, exit_load is years old, NAV is daily
    assert len(fetched) == 1  # one run, one fetch timestamp
    assert all(r["source_as_of"] not in fetched for r in stored.values())
    assert all(r["source_as_of"] and r["fetched_at"] for r in stored.values())


def test_each_page_is_fetched_once_despite_seven_doc_types(ingest, db_path):
    """P2 exit criterion — 7 facts per page, 1 request per page."""
    report = ingest(db_path)
    assert report.requests_made == len(SCHEME_IDS)


def test_rerunning_immediately_changes_nothing(ingest, db_path):
    ingest(db_path)
    report = ingest(db_path)

    assert report.status == "success"
    assert report.summary.unchanged == 35
    assert report.summary.changed == 0
    assert report.summary.refreshed == 0
    assert report.cards_to_embed() == []


# -- exit criterion: out-of-corpus facts never reach the registry ----------


def test_out_of_corpus_facts_are_absent_from_the_registry(ingest, db_path):
    """P0.4 declared riskometer and the two process docs unobtainable."""
    ingest(db_path)
    doc_types = {r["doc_type"] for r in documents(db_path).values()}

    excluded = {f.fact for f in SOURCES.out_of_corpus}
    assert excluded == {"riskometer", "statement_process", "capital_gains_process"}
    assert doc_types & excluded == set()
    assert doc_types == {
        "nav",
        "expense_ratio",
        "exit_load",
        "holdings",
        "min_sip",
        "lock_in",
        "benchmark",
    }


# -- exit criterion: an undated fact is rejected with a clear log line -----


def test_a_fact_with_no_extractable_date_is_rejected_and_logged(ingest, db_path, caplog):
    """P2 exit criterion. An uncitable fact is unusable (ARCH §11)."""
    undated = copy.deepcopy(payload("mo_elss"))
    del undated["historic_fund_expense"]  # nothing left to date expense_ratio with

    with caplog.at_level(logging.INFO):
        report = ingest(db_path, ["mo_elss"], payloads={"mo_elss": undated})

    assert report.status == "success"  # a coverage gap is not a run failure
    assert "mo_elss:expense_ratio" not in documents(db_path)
    assert len(documents(db_path)) == 6

    gap = report.missing[0]
    assert gap.doc_id == "mo_elss:expense_ratio"
    assert "no historic_fund_expense to date the value" in gap.reason
    assert not gap.regression

    messages = [r.message for r in caplog.records]
    assert "fact rejected" in messages  # from the parser
    assert "fact unavailable" in messages  # from the pipeline


def test_a_fact_that_disappears_after_being_stored_is_flagged_as_a_regression(
    ingest, db_path, caplog
):
    """The registry is what makes "gap" and "regression" distinguishable."""
    ingest(db_path, ["mo_elss"])

    undated = copy.deepcopy(payload("mo_elss"))
    del undated["historic_fund_expense"]
    with caplog.at_level(logging.WARNING):
        report = ingest(db_path, ["mo_elss"], payloads={"mo_elss": undated})

    assert [m.doc_id for m in report.regressions] == ["mo_elss:expense_ratio"]
    assert "fact regressed" in [r.message for r in caplog.records]

    # The stored row survives untouched — it is dated, so it ages toward the
    # §7.3 freshness gate rather than vanishing mid-flight.
    assert documents(db_path)["mo_elss:expense_ratio"]["source_as_of"] == "2026-08-28"
    assert report.status == "success"


# -- failure handling ------------------------------------------------------


def test_a_failed_scheme_writes_nothing_at_all(ingest, db_path):
    """The local half of ARCH §8.4 / edge case S-03.

    Four schemes parsed cleanly and are still discarded. Writing them here while
    CI's commit step discards them on the same failure would make a laptop and a
    runner disagree about what a failed run leaves behind.
    """
    report = ingest(db_path, fail={"mo_elss": 404})

    assert report.status == "failed"
    assert report.exit_code == 1
    assert not report.committed
    assert [s.scheme_id for s in report.failed_schemes] == ["mo_elss"]
    assert "404" in report.failed_schemes[0].error
    assert documents(db_path) == {}  # not even the four that succeeded


def test_a_failed_scheme_leaves_the_previous_registry_standing(ingest, db_path):
    ingest(db_path)
    before = documents(db_path)

    report = ingest(db_path, fail={"mo_elss": 500})

    assert report.status == "failed"
    assert documents(db_path) == before  # byte-for-byte, fetched_at included


def test_every_scheme_is_attempted_even_after_one_fails(ingest, db_path):
    """One run should surface every problem, not just the first."""
    report = ingest(db_path, fail={"mo_elss": 404, "mo_next50": 500})

    assert len(report.schemes) == len(SCHEME_IDS)
    assert {s.scheme_id for s in report.failed_schemes} == {"mo_elss", "mo_next50"}
    assert all(s.ok for s in report.schemes if s.scheme_id not in {"mo_elss", "mo_next50"})


def test_a_bot_block_page_fails_the_scheme_rather_than_parsing_as_empty(ingest, db_path):
    """I-01/I-02: a 200 with a tiny body is a block, not a page with no NAV."""
    report = ingest(bodies={"mo_elss": "<html>are you a robot?</html>"})

    assert "SuspiciousContent" in report.failed_schemes[0].error
    assert documents(db_path) == {}


def test_a_conflict_fails_the_run_but_the_quarantine_is_written(ingest, db_path):
    """I-09. The rejection *is* the write, so the transaction commits — but the
    run goes red so P6.7 raises an issue and the commit step never publishes."""
    ingest(db_path, ["mo_elss"])

    tampered = copy.deepcopy(payload("mo_elss"))
    tampered["expense_ratio"] = 1.75  # new value, same as_on_date
    report = ingest(db_path, ["mo_elss"], payloads={"mo_elss": tampered})

    assert report.status == "failed"
    assert report.exit_code == 1
    assert report.committed  # the quarantine was written
    assert [d.doc_id for d in report.conflicts] == ["mo_elss:expense_ratio"]

    row = documents(db_path)["mo_elss:expense_ratio"]
    assert row["status"] == "failed"
    assert row["source_as_of"] == "2026-08-28"  # old value under its own date


# -- run log ---------------------------------------------------------------


def test_the_run_log_records_a_successful_run(ingest, db_path):
    report = ingest(db_path)
    logged = runs(db_path)

    assert len(logged) == 1
    row = logged[0]
    assert row["run_id"] == report.run_id
    assert row["status"] == "success"
    assert row["finished_at"] is not None
    assert row["sources_attempted"] == 35
    assert row["sources_changed"] == 35
    assert row["sources_failed"] == 0
    assert row["error_detail"] is None


def test_the_run_log_records_a_failed_run_with_its_reason(ingest, db_path):
    """A failed run that leaves no evidence of failing is worse than useless."""
    ingest(db_path, fail={"mo_elss": 404})
    row = runs(db_path)[0]

    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert "mo_elss" in row["error_detail"]
    assert row["sources_attempted"] == 35
    assert row["sources_failed"] == 35  # every fact on every scheme was discarded


def test_run_ids_are_unique_and_chronologically_sortable(ingest, db_path):
    """The timestamp prefix orders runs; the suffix keeps two in the same second
    from colliding on the primary key."""
    ids = [ingest(db_path).run_id for _ in range(3)]

    assert len(set(ids)) == 3
    stamps = [i.split("-")[0] for i in ids]
    assert stamps == sorted(stamps)
    assert {r["run_id"] for r in runs(db_path)} == set(ids)


def test_every_requested_fact_is_accounted_for_on_a_run_that_wrote(ingest, db_path):
    """Invariant: attempted == the five change outcomes + facts not yielded."""
    undated = copy.deepcopy(payload("mo_elss"))
    del undated["historic_fund_expense"]
    report = ingest(db_path, payloads={"mo_elss": undated})

    s = report.summary
    accounted = (
        s.new + s.unchanged + s.refreshed + s.changed + len(s.conflicts) + len(report.missing)
    )
    assert report.run_row()["sources_attempted"] == accounted == 35
    assert report.run_row()["sources_failed"] == 1  # the undated expense_ratio


def test_a_run_that_wrote_nothing_counts_every_fact_as_failed(ingest, db_path):
    """Reporting 7 of 35 failed would imply the other 28 were refreshed."""
    report = ingest(db_path, fail={"mo_next50": 500})

    assert not report.committed
    counts = report.run_row()
    assert counts["sources_attempted"] == counts["sources_failed"] == 35
    assert counts["sources_changed"] == 0


# -- subset runs -----------------------------------------------------------


def test_a_single_scheme_run_touches_only_that_scheme(ingest, db_path):
    """Backs the P6 `workflow_dispatch` scheme_id path and the 2.9 --scheme flag."""
    ingest(db_path)
    before = documents(db_path)

    moved = copy.deepcopy(payload("mo_elss"))
    moved["nav"] = 99.1234
    moved["nav_date"] = "29-Aug-2026"
    report = ingest(db_path, ["mo_elss"], payloads={"mo_elss": moved})

    assert report.requests_made == 1
    assert report.run_row()["sources_attempted"] == 7
    after = documents(db_path)
    for doc_id, row in after.items():
        if doc_id.startswith("mo_elss:"):
            continue
        assert row == before[doc_id]  # untouched, fetched_at included


def test_an_unknown_scheme_id_fails_before_any_network_traffic(db_path, groww_fetcher):
    """The CLI's --scheme choices catch this first; the library must too."""
    fetcher = groww_fetcher()
    with pytest.raises(KeyError):
        run(["not_a_scheme"], db_path=db_path, sources=SOURCES, fetcher=fetcher)

    assert fetcher.stats.requests_made == 0
    assert not db_path.exists()  # no schema created, no run logged


# -- handoff to Phase 3 ----------------------------------------------------


def test_the_report_separates_cards_to_embed_from_cards_to_restamp(ingest, db_path):
    """P3 consumes these two lists; they must not overlap."""
    ingest(db_path)

    moved = copy.deepcopy(payload("mo_elss"))
    moved["nav"] = 99.1234
    moved["nav_date"] = "29-Aug-2026"  # advances min_sip / lock_in / benchmark too
    report = ingest(db_path, ["mo_elss"], payloads={"mo_elss": moved})

    embed = {c.doc_id for c in report.cards_to_embed()}
    restamp = {c.doc_id for c in report.cards_to_restamp()}

    assert embed == {"mo_elss:nav"}
    assert restamp == {"mo_elss:min_sip", "mo_elss:lock_in", "mo_elss:benchmark"}
    assert embed & restamp == set()


# -- reporting -------------------------------------------------------------


def test_format_report_surfaces_the_things_an_operator_must_see(ingest, db_path):
    ingest(db_path, ["mo_elss"])
    tampered = copy.deepcopy(payload("mo_elss"))
    tampered["expense_ratio"] = 1.75
    text = format_report(ingest(db_path, ["mo_elss"], payloads={"mo_elss": tampered}))

    assert "[FAILED]" in text
    assert "CONFLICT" in text
    assert "mo_elss:expense_ratio" in text


def test_format_report_says_when_nothing_was_written(ingest, db_path):
    text = format_report(ingest(db_path, fail={"mo_elss": 404}))
    assert "registry NOT written" in text
    assert "FAILED" in text
