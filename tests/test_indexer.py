"""The P2 → P3 seam: pipeline run → index (P3.4 wiring).

Drives the *whole* stack — fetch through embed — over `httpx.MockTransport` and
the fake embedder, so nothing here touches the network or downloads a model.
"""

from __future__ import annotations

import copy
import json

import pytest

from mf_faq.ingest import cli, pipeline
from mf_faq.retrieval.indexer import registry_doc_ids, sync_from_run
from tests.conftest import groww_payload


@pytest.fixture
def run_and_index(tmp_path, groww_fetcher, vector_store):
    """Run the real pipeline against fixtures, then sync the index."""

    def go(*, scheme_ids=None, fetcher=None, dry_run=False, sync=True, **sync_kwargs):
        report = pipeline.run(
            scheme_ids,
            db_path=tmp_path / "registry.db",
            fetcher=fetcher or groww_fetcher(),
            dry_run=dry_run,
        )
        result = None
        if sync:
            result = sync_from_run(
                report, db_path=tmp_path / "registry.db", store=vector_store, **sync_kwargs
            )
        return report, result

    return go


class TestHappyPath:
    def test_a_first_run_indexes_the_whole_corpus(self, run_and_index, vector_store):
        report, sync = run_and_index()
        assert report.exit_code == 0
        assert sync.embedded == 35
        assert sync.repaired == 0
        assert vector_store.count() == 35

    def test_a_second_run_embeds_nothing(self, run_and_index, vector_store):
        run_and_index()
        report, sync = run_and_index()
        assert report.summary.unchanged == 35
        assert sync.embedded == 0
        assert sync.unchanged == 35
        assert vector_store.count() == 35

    def test_the_index_holds_exactly_what_the_registry_holds(
        self, run_and_index, tmp_path, vector_store
    ):
        """The invariant the whole sync exists to maintain."""
        run_and_index()
        schemes = ["mo_large_midcap", "mo_bse_value", "mo_elss", "mo_next50", "mo_bse_fin"]
        indexed = {m["doc_id"] for m in vector_store.indexed_state().values()}
        assert indexed == registry_doc_ids(tmp_path / "registry.db", schemes)
        assert len(indexed) == 35

    def test_out_of_corpus_facts_never_reach_the_index(self, run_and_index, vector_store):
        """P0.4 exclusions, one layer further down than the P2 registry check."""
        run_and_index()
        types = {m["doc_type"] for m in vector_store.indexed_state().values()}
        assert types == {
            "nav",
            "expense_ratio",
            "exit_load",
            "holdings",
            "min_sip",
            "lock_in",
            "benchmark",
        }
        assert not (types & {"riskometer", "statement_process", "capital_gains_process"})


class TestWhatMustNotBeIndexed:
    def test_a_dry_run_raises_rather_than_writing(self, run_and_index, vector_store):
        with pytest.raises(ValueError, match="dry run"):
            run_and_index(dry_run=True)
        assert vector_store.count() == 0

    def test_a_failed_run_raises_rather_than_writing(
        self, run_and_index, groww_fetcher, vector_store
    ):
        """The registry was not written either — indexing would put it ahead."""
        with pytest.raises(ValueError, match="failed run"):
            run_and_index(fetcher=groww_fetcher(fail={"mo_elss": 500}))
        assert vector_store.count() == 0

    def test_a_conflicted_fact_keeps_its_previous_text(
        self, run_and_index, groww_fetcher, vector_store
    ):
        """I-09: the registry kept the old value, so the index must too."""
        run_and_index()
        before = vector_store.indexed_state()["mo_elss:expense_ratio#0"]

        # Move the expense ratio while its as_on_date stands still.
        payload = copy.deepcopy(groww_payload("mo_elss"))
        payload["expense_ratio"] = 1.75

        report, sync = run_and_index(fetcher=groww_fetcher(payloads={"mo_elss": payload}))
        assert report.conflicts
        assert report.exit_code == 1
        assert sync.skipped_conflicts == 1
        assert sync.embedded == 0
        assert vector_store.indexed_state()["mo_elss:expense_ratio#0"] == before


class TestDriftRepair:
    def test_a_deleted_index_is_rebuilt_by_the_next_ordinary_run(self, run_and_index, vector_store):
        """The registry is settled, so only a convergent sync can recover this."""
        run_and_index()
        vector_store.drop()

        report, sync = run_and_index()
        assert report.summary.unchanged == 35  # the registry saw no reason to embed
        assert sync.embedded == 35  # sync did it anyway
        assert sync.repaired == 35
        assert vector_store.count() == 35


class TestScoping:
    def test_a_single_scheme_run_leaves_the_others_indexed(self, run_and_index, vector_store):
        run_and_index()
        report, sync = run_and_index(scheme_ids=["mo_elss"])
        assert sync.deleted == 0
        assert sync.unchanged == 7
        assert vector_store.count() == 35

    def test_registry_doc_ids_narrows_by_scheme(self, run_and_index, tmp_path):
        run_and_index()
        ids = registry_doc_ids(tmp_path / "registry.db", ["mo_elss"])
        assert len(ids) == 7
        assert all(i.startswith("mo_elss:") for i in ids)

    def test_registry_doc_ids_on_a_missing_db_is_empty(self, tmp_path):
        assert registry_doc_ids(tmp_path / "nope.db", ["mo_elss"]) == set()

    def test_registry_doc_ids_with_no_schemes_is_empty(self, tmp_path):
        assert registry_doc_ids(tmp_path / "registry.db", []) == set()


class TestCliWiring:
    """`--no-index`, `--dry-run` and failures must all skip the embed step."""

    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch, groww_fetcher):
        monkeypatch.setattr(pipeline, "build_fetcher", lambda *a, **k: groww_fetcher())

    def _argv(self, tmp_path, *extra):
        return ["--all", "--db", str(tmp_path / "registry.db"), *extra]

    def test_no_index_skips_embedding(self, tmp_path, capsys, monkeypatch):
        called = []
        monkeypatch.setattr(
            "mf_faq.retrieval.indexer.sync_from_run", lambda *a, **k: called.append(1)
        )
        assert cli.main(self._argv(tmp_path, "--no-index")) == 0
        assert not called
        assert "index: skipped (--no-index)" in capsys.readouterr().out

    def test_dry_run_skips_embedding(self, tmp_path, capsys):
        assert cli.main(self._argv(tmp_path, "--dry-run")) == 0
        assert "index: not written (dry run)" in capsys.readouterr().out

    def test_a_failed_run_skips_embedding(self, tmp_path, capsys, monkeypatch, groww_fetcher):
        monkeypatch.setattr(
            pipeline, "build_fetcher", lambda *a, **k: groww_fetcher(fail={"mo_elss": 500})
        )
        assert cli.main(self._argv(tmp_path)) == 1
        assert "index: not written (run failed)" in capsys.readouterr().out

    def test_an_index_failure_fails_the_run(self, tmp_path, capsys, monkeypatch):
        """A committed registry with a stale index must not publish."""

        def boom(*a, **k):
            raise RuntimeError("chroma exploded")

        monkeypatch.setattr("mf_faq.retrieval.indexer.sync_from_run", boom)
        assert cli.main(self._argv(tmp_path)) == 1
        assert "INDEX FAILED" in capsys.readouterr().out

    def test_json_output_carries_the_sync_counters(self, tmp_path, capsys, monkeypatch):
        from mf_faq.retrieval.store import SyncReport

        monkeypatch.setattr(
            "mf_faq.retrieval.indexer.sync_from_run",
            lambda *a, **k: SyncReport(embedded=35),
        )
        assert cli.main(self._argv(tmp_path, "--json")) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["index"]["embedded"] == 35

    def test_json_index_is_null_when_skipped(self, tmp_path, capsys):
        assert cli.main(self._argv(tmp_path, "--no-index", "--json")) == 0
        assert json.loads(capsys.readouterr().out)["index"] is None

    def test_no_index_and_rebuild_index_are_mutually_exclusive(self, tmp_path):
        with pytest.raises(SystemExit):
            cli.main(self._argv(tmp_path, "--no-index", "--rebuild-index"))

    def test_rebuild_index_takes_the_rebuild_path(self, tmp_path, monkeypatch):
        """`--rebuild-index` must call `rebuild`, not the ordinary sync."""
        from mf_faq.retrieval.store import SyncReport

        called = []
        monkeypatch.setattr(
            "mf_faq.retrieval.indexer.rebuild",
            lambda *a, **k: (called.append("rebuild"), SyncReport(embedded=35))[1],
        )
        monkeypatch.setattr(
            "mf_faq.retrieval.indexer.sync_from_run",
            lambda *a, **k: called.append("sync"),
        )
        assert cli.main(self._argv(tmp_path, "--rebuild-index")) == 0
        assert called == ["rebuild"]

    def test_rebuild_drops_the_collection_before_re_embedding(
        self, run_and_index, vector_store, tmp_path, groww_fetcher
    ):
        from mf_faq.retrieval.indexer import rebuild

        report, _ = run_and_index()
        vector_store.upsert([])  # collection exists
        stale = "zz_gone:nav#0"
        vector_store.collection.upsert(
            ids=[stale],
            embeddings=[[0.0] * vector_store.embedder.dimension],
            documents=["orphan"],
            metadatas=[{"doc_id": "zz_gone:nav", "scheme_id": "zz_gone"}],
        )
        assert vector_store.count() == 36

        sync = rebuild(report, db_path=tmp_path / "registry.db", store=vector_store)
        assert sync.embedded == 35
        assert vector_store.count() == 35  # the orphan is gone with the collection
