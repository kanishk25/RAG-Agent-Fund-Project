"""The retrieval inspection CLI (`python -m mf_faq.retrieval`).

Read-only is the promise, so it is asserted rather than described: one test
forbids writes to the collection for the duration of a run.
"""

from __future__ import annotations

import json

import pytest

from mf_faq.retrieval import cli
from mf_faq.retrieval.search import Searcher

QUERY = "What is the NAV of Motilal Oswal ELSS Tax Saver Fund?"


@pytest.fixture(autouse=True)
def wired(monkeypatch, populated_store):
    """Point `build_searcher` at the offline fixture store."""
    monkeypatch.setattr(
        "mf_faq.retrieval.search.build_searcher",
        lambda **kw: Searcher(populated_store, **{k: v for k, v in kw.items() if v is not None}),
    )
    return populated_store


class TestUsage:
    def test_a_missing_query_exits_2(self, capsys):
        assert cli.main([]) == 2
        assert "a query is required" in capsys.readouterr().err

    def test_a_blank_query_exits_2(self, capsys):
        assert cli.main(["   "]) == 2

    def test_help_does_not_load_the_embedding_stack(self, capsys):
        """`--help` must not pay for torch."""
        with pytest.raises(SystemExit) as exc:
            cli.main(["--help"])
        assert exc.value.code == 0
        assert "Facts-only" in capsys.readouterr().out


class TestOutput:
    def test_prints_the_resolution_and_chunks(self, capsys):
        assert cli.main([QUERY]) == 0
        out = capsys.readouterr().out
        assert "mo_elss" in out
        assert "outcome:" in out
        assert "embedded:" in out  # the stripped query is shown

    def test_json_is_parseable(self, capsys):
        assert cli.main([QUERY, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["scheme_id"] == "mo_elss"
        assert payload["chunks"]
        assert all(c["doc_id"].startswith("mo_elss:") for c in payload["chunks"])

    def test_json_is_not_polluted_by_logs(self, capsys):
        assert cli.main([QUERY, "--json", "--log-level", "DEBUG"]) == 0
        json.loads(capsys.readouterr().out)

    def test_an_ambiguous_query_reports_its_candidates(self, capsys):
        assert cli.main(["expense ratio of the Motilal Oswal BSE index fund", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["outcome"] == "scheme_ambiguous"
        assert set(payload["candidates"]) == {"mo_bse_value", "mo_bse_fin"}
        assert payload["chunks"] == []

    def test_no_floor_shows_what_the_floor_suppresses(self, capsys):
        cli.main([QUERY, "--floor", "1.1", "--json"])
        suppressed = json.loads(capsys.readouterr().out)
        assert suppressed["chunks"] == []
        assert suppressed["discarded"]

        cli.main([QUERY, "--no-floor", "--json"])
        assert json.loads(capsys.readouterr().out)["chunks"]

    def test_top_k_is_honoured(self, capsys):
        cli.main([QUERY, "--top-k", "2", "--no-floor", "--json"])
        assert len(json.loads(capsys.readouterr().out)["chunks"]) == 2


class TestEmptyIndex:
    def test_reports_an_empty_index_and_exits_1(self, monkeypatch, tmp_path, fake_embedder, capsys):
        """R-03 — a fresh store, not the populated fixture the others share."""
        from mf_faq.retrieval.store import VectorStore

        empty = VectorStore(tmp_path / "empty", "mf_facts_empty", fake_embedder)
        monkeypatch.setattr("mf_faq.retrieval.search.build_searcher", lambda **kw: Searcher(empty))
        assert cli.main([QUERY]) == 1
        assert "is empty" in capsys.readouterr().err


class TestReadOnly:
    def test_no_write_reaches_the_collection(self, wired, monkeypatch):
        """The promise in the docstring, made a test."""
        for method in ("upsert", "update", "delete", "add"):
            monkeypatch.setattr(
                wired.collection,
                method,
                lambda *a, **k: pytest.fail("the inspection CLI wrote to the index"),
            )
        assert cli.main([QUERY]) == 0

    def test_module_does_not_import_the_writing_layers(self):
        """Checks import statements, not prose — the docstring discusses these."""
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path(cli.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = {
            "mf_faq.retrieval.indexer",
            "mf_faq.ingest.pipeline",
            "mf_faq.ingest.change",
            "mf_faq.db",
        }
        assert not (imported & forbidden), f"the inspection CLI imports {imported & forbidden}"
