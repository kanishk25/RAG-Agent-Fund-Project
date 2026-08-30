"""Ingestion CLI tests (P2.9).

Most of these go through `main(argv)` end to end with `build_fetcher`
monkeypatched to the offline harness, so argument parsing, the pipeline, the
registry write and the rendering are all exercised together. A CLI tested only
against a stubbed `run()` proves the arguments parse and nothing else.

The load-bearing tests are the two about the exit code. P6 hangs the entire
publish decision on it — a non-zero exit skips the commit step and the
repository keeps the last good index (ARCH §8.4) — so a CLI that exits 0 on a
failed run would publish a broken index every time.
"""

from __future__ import annotations

import contextlib
import copy
import json

import pytest

from mf_faq import db
from mf_faq.ingest import cli
from tests.conftest import groww_payload

SCHEME_IDS = ["mo_large_midcap", "mo_bse_value", "mo_elss", "mo_next50", "mo_bse_fin"]


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "registry.db"


@pytest.fixture
def run_cli(db_path, groww_fetcher, monkeypatch):
    """Invoke `main(argv)` with the network replaced by the fixtures."""

    def _run(*argv, payloads=None, fail=None, bodies=None):
        monkeypatch.setattr(
            "mf_faq.ingest.pipeline.build_fetcher",
            lambda *a, **k: groww_fetcher(payloads=payloads, fail=fail, bodies=bodies),
        )
        return cli.main([*argv, "--db", str(db_path)])

    return _run


def documents(db_path) -> dict[str, dict]:
    if not db_path.exists():
        return {}
    with contextlib.closing(db.connect(db_path)) as conn:
        return {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM documents")}


def runs(db_path) -> list[dict]:
    if not db_path.exists():
        return []
    with contextlib.closing(db.connect(db_path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM runs")]


# -- targets ---------------------------------------------------------------


def test_all_refreshes_the_whole_corpus(run_cli, db_path, capsys):
    assert run_cli("--all") == 0

    stored = documents(db_path)
    assert len(stored) == 35
    assert {r["scheme_id"] for r in stored.values()} == set(SCHEME_IDS)
    assert "SUCCESS" in capsys.readouterr().out


def test_scheme_refreshes_only_that_scheme(run_cli, db_path):
    assert run_cli("--scheme", "mo_elss") == 0

    stored = documents(db_path)
    assert len(stored) == 7
    assert {r["scheme_id"] for r in stored.values()} == {"mo_elss"}


def test_scheme_is_repeatable(run_cli, db_path):
    assert run_cli("--scheme", "mo_elss", "--scheme", "mo_next50") == 0
    assert {r["scheme_id"] for r in documents(db_path).values()} == {"mo_elss", "mo_next50"}


# -- the refusal to crawl by default ---------------------------------------


def test_a_bare_invocation_refuses_to_crawl(db_path, capsys):
    """The obvious accident is someone running the CLI to see what it does and
    putting real traffic on groww.in to find out."""
    assert cli.main(["--db", str(db_path)]) == cli.EXIT_USAGE

    err = capsys.readouterr().err
    assert "choose a target" in err
    assert "Refusing to crawl" in err
    assert not db_path.exists()  # nothing was touched


def test_all_and_scheme_are_mutually_exclusive(db_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--all", "--scheme", "mo_elss", "--db", str(db_path)])
    assert exc.value.code == cli.EXIT_USAGE


def test_an_unknown_scheme_is_rejected_by_argparse(db_path, capsys):
    """Rejected at parse time, so the typo never reaches the fetcher."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["--scheme", "mo_nonexistent", "--db", str(db_path)])

    assert exc.value.code == cli.EXIT_USAGE
    assert "mo_nonexistent" in capsys.readouterr().err
    assert not db_path.exists()


# -- the exit code is the contract -----------------------------------------


def test_a_clean_run_exits_zero(run_cli):
    assert run_cli("--all") == 0


def test_a_failed_scheme_exits_non_zero_and_writes_nothing(run_cli, db_path, capsys):
    """P6 skips the commit step on this, so the repo keeps the last good index."""
    assert run_cli("--all", fail={"mo_elss": 404}) == 1

    assert documents(db_path) == {}
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "registry NOT written" in out


def test_an_unchanged_rerun_still_exits_zero(run_cli, db_path, capsys):
    """A no-change day must be green, not a failure (S-01)."""
    assert run_cli("--all") == 0
    assert run_cli("--all") == 0

    out = capsys.readouterr().out
    assert "unchanged 35" in out
    assert len(runs(db_path)) == 2


def test_a_conflict_exits_non_zero(run_cli, db_path, capsys):
    run_cli("--scheme", "mo_elss")

    tampered = copy.deepcopy(groww_payload("mo_elss"))
    tampered["expense_ratio"] = 1.75  # I-09: new value, unmoved date
    assert run_cli("--scheme", "mo_elss", payloads={"mo_elss": tampered}) == 1

    assert "CONFLICT" in capsys.readouterr().out
    assert documents(db_path)["mo_elss:expense_ratio"]["status"] == "failed"


# -- dry run ---------------------------------------------------------------


def test_dry_run_touches_nothing_on_disk(run_cli, db_path, capsys):
    """Not even the schema: a preview did not happen, so the run log must not
    claim it did."""
    assert run_cli("--all", "--dry-run") == 0

    assert not db_path.exists()
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "new 35" in out  # it still reports what a real run would do


def test_dry_run_previews_against_an_existing_registry(run_cli, db_path, capsys):
    run_cli("--all")
    before = documents(db_path)
    capsys.readouterr()

    moved = copy.deepcopy(groww_payload("mo_elss"))
    moved["nav"] = 99.1234
    moved["nav_date"] = "29-Aug-2026"
    assert run_cli("--scheme", "mo_elss", "--dry-run", payloads={"mo_elss": moved}) == 0

    out = capsys.readouterr().out
    assert "changed 1" in out  # the NAV would move
    assert "refreshed 3" in out  # min_sip / lock_in / benchmark would be re-dated
    assert documents(db_path) == before  # but nothing did move
    assert len(runs(db_path)) == 1  # and no second run was logged


def test_dry_run_still_reports_a_conflict_and_exits_non_zero(run_cli, db_path):
    """The point of previewing: see the I-09 rejection before it is written."""
    run_cli("--scheme", "mo_elss")

    tampered = copy.deepcopy(groww_payload("mo_elss"))
    tampered["expense_ratio"] = 1.75
    assert run_cli("--scheme", "mo_elss", "--dry-run", payloads={"mo_elss": tampered}) == 1

    assert documents(db_path)["mo_elss:expense_ratio"]["status"] == "ok"  # not quarantined


# -- JSON output -----------------------------------------------------------


def test_json_output_is_parseable_and_carries_the_run_outcome(run_cli, capsys):
    assert run_cli("--all", "--json") == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "success"
    assert report["exit_code"] == 0
    assert report["committed"] is True
    assert report["counts"]["sources_attempted"] == 35
    assert report["counts"]["new"] == 35
    assert len(report["to_embed"]) == 35
    assert len(report["schemes"]) == 5
    assert report["conflicts"] == []


def test_json_output_is_not_polluted_by_log_lines(run_cli, capsys):
    """`--json` must be pipeable into jq without a --log-level incantation."""
    run_cli("--all", "--json", fail={"mo_elss": 500})

    captured = capsys.readouterr()
    report = json.loads(captured.out)  # raises if a log line landed on stdout
    assert report["status"] == "failed"
    assert report["exit_code"] == 1
    assert report["committed"] is False


def test_json_names_the_conflicting_documents(run_cli, capsys):
    run_cli("--scheme", "mo_elss")
    capsys.readouterr()

    tampered = copy.deepcopy(groww_payload("mo_elss"))
    tampered["expense_ratio"] = 1.75
    run_cli("--scheme", "mo_elss", "--json", payloads={"mo_elss": tampered})

    report = json.loads(capsys.readouterr().out)
    assert [c["doc_id"] for c in report["conflicts"]] == ["mo_elss:expense_ratio"]
    assert "I-09" in report["conflicts"][0]["reason"]


def test_json_names_missing_facts_and_marks_regressions(run_cli, capsys):
    run_cli("--scheme", "mo_elss")
    capsys.readouterr()

    undated = copy.deepcopy(groww_payload("mo_elss"))
    del undated["historic_fund_expense"]  # nothing left to date expense_ratio
    run_cli("--scheme", "mo_elss", "--json", payloads={"mo_elss": undated})

    report = json.loads(capsys.readouterr().out)
    assert report["missing"][0]["doc_type"] == "expense_ratio"
    assert report["missing"][0]["regression"] is True
    assert report["status"] == "success"  # a coverage gap is not a run failure


# -- wiring ----------------------------------------------------------------


def test_the_module_entry_point_is_importable():
    """`python -m mf_faq.ingest` and the `mf-faq-ingest` console script must
    both resolve — pyproject has pointed at `cli:main` since P1.1."""
    import importlib.util

    assert importlib.util.find_spec("mf_faq.ingest.__main__") is not None
    assert callable(cli.main)


def test_help_names_every_scheme(capsys):
    """A wrong --scheme is the likeliest operator error; --help must answer it."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    for scheme_id in SCHEME_IDS:
        assert scheme_id in out
