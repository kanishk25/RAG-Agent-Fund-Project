"""Chroma store and the convergent sync (P3.4).

Uses the fake embedder throughout — nothing here is about embedding quality,
and a 90 MB download to test a deletion rule would be a bad trade. Recall
against the real model lives in `test_retrieval_eval.py`.
"""

from __future__ import annotations

import dataclasses

import pytest

from mf_faq.retrieval.chunk import chunk_cards
from mf_faq.retrieval.store import RetrievedChunk, SyncReport, VectorStore

ALL_SCHEMES = ["mo_large_midcap", "mo_bse_value", "mo_elss", "mo_next50", "mo_bse_fin"]


def _doc_ids(cards) -> set[str]:
    return {c.doc_id for c in cards}


def _sync(store, cards, **over):
    kwargs = dict(scope=ALL_SCHEMES, registry_doc_ids=_doc_ids(cards))
    kwargs.update(over)
    return store.sync(cards, **kwargs)


class TestCollectionSetup:
    def test_collection_is_cosine_space(self, vector_store):
        """L2 would make the similarity floor a meaningless number."""
        assert vector_store.collection.metadata["hnsw:space"] == "cosine"

    def test_empty_store_counts_zero(self, vector_store):
        assert vector_store.count() == 0

    def test_query_on_an_empty_collection_returns_nothing(self, vector_store, fake_embedder):
        vec = fake_embedder.encode_query("anything")
        assert vector_store.query(vec, scheme_id="mo_elss", top_k=4) == []

    def test_directory_is_created_on_demand(self, tmp_path, fake_embedder):
        store = VectorStore(tmp_path / "nested" / "chroma", "mf_facts_test", fake_embedder)
        assert store.count() == 0
        assert (tmp_path / "nested" / "chroma").exists()


class TestTheCommittedIndexIsProtected:
    """The suite must never write to `data/chroma`, which ARCH §8.2 commits.

    Pins the conftest guard. Without it, `tests/test_cli.py` drives the ingest
    CLI straight into the published index — and it did, until a full-suite run
    was checked rather than a per-file one.
    """

    def test_the_default_store_path_is_not_the_repo_index(self):
        from mf_faq.settings import REPO_ROOT

        assert VectorStore.open().path != REPO_ROOT / "data" / "chroma"

    def test_the_ingest_cli_can_redirect_the_index(self):
        """`--db` without `--chroma-dir` would redirect one artifact, not both."""
        from mf_faq.ingest.cli import build_parser

        args = build_parser(["mo_elss"]).parse_args(
            ["--all", "--db", "/tmp/x.db", "--chroma-dir", "/tmp/x"]
        )
        assert str(args.chroma_dir) == "/tmp/x"


class TestWrites:
    def test_upsert_writes_every_chunk(self, vector_store, corpus_cards):
        vector_store.upsert(chunk_cards(corpus_cards))
        assert vector_store.count() == 35

    def test_upsert_is_idempotent(self, vector_store, corpus_cards):
        chunks = chunk_cards(corpus_cards)
        vector_store.upsert(chunks)
        vector_store.upsert(chunks)
        assert vector_store.count() == 35

    def test_upsert_of_nothing_is_a_no_op(self, vector_store):
        vector_store.upsert([])
        assert vector_store.count() == 0

    def test_the_vector_comes_from_embed_text_not_the_served_text(
        self, vector_store, corpus_cards, fake_embedder
    ):
        chunks = chunk_cards(corpus_cards)
        vector_store.upsert(chunks)
        chunk = next(c for c in chunks if c.doc_id == "mo_elss:nav")
        stored_doc = vector_store.collection.get(ids=[chunk.chunk_id])["documents"][0]
        assert stored_doc == chunk.text  # served text is complete

        by_embed = vector_store.query(
            fake_embedder.encode_query(chunk.embed_text), scheme_id="mo_elss", top_k=1
        )[0].similarity
        by_served = vector_store.query(
            fake_embedder.encode_query(chunk.text), scheme_id="mo_elss", top_k=1
        )[0].similarity
        assert by_embed > by_served

    def test_metadata_survives_the_round_trip(self, populated_store, corpus_cards):
        stored = populated_store.indexed_state()
        chunk = chunk_cards(corpus_cards)[0]
        assert stored[chunk.chunk_id] == chunk.metadata()

    def test_restamp_replaces_metadata_wholesale(self, populated_store, corpus_cards):
        """A partial update would drop card_hash and fake a divergence next sync."""
        chunk = chunk_cards(corpus_cards)[0]
        moved = dataclasses.replace(chunk, source_as_of="2026-09-01")
        populated_store.restamp([moved])
        stored = populated_store.indexed_state()[chunk.chunk_id]
        assert stored["source_as_of"] == "2026-09-01"
        assert stored["card_hash"] == chunk.card_hash  # not dropped

    def test_delete_removes_chunks(self, populated_store, corpus_cards):
        chunk = chunk_cards(corpus_cards)[0]
        populated_store.delete([chunk.chunk_id])
        assert populated_store.count() == 34

    def test_drop_empties_the_collection(self, populated_store):
        populated_store.drop()
        assert populated_store.count() == 0

    def test_drop_on_a_missing_collection_does_not_raise(self, vector_store):
        vector_store.drop()
        vector_store.drop()


class TestIndexedState:
    def test_narrows_to_the_requested_schemes(self, populated_store):
        state = populated_store.indexed_state(["mo_elss"])
        assert len(state) == 7
        assert all(m["scheme_id"] == "mo_elss" for m in state.values())

    def test_two_schemes_use_the_in_form(self, populated_store):
        state = populated_store.indexed_state(["mo_elss", "mo_next50"])
        assert len(state) == 14

    def test_empty_scope_returns_nothing(self, populated_store):
        assert populated_store.indexed_state([]) == {}

    def test_none_scope_returns_everything(self, populated_store):
        assert len(populated_store.indexed_state(None)) == 35


class TestSyncConverges:
    """The property the whole design of `sync` exists for."""

    def test_first_sync_embeds_the_whole_corpus(self, vector_store, corpus_cards):
        report = _sync(vector_store, corpus_cards)
        assert report.embedded == 35
        assert report.unchanged == report.deleted == 0
        assert vector_store.count() == 35

    def test_second_sync_embeds_nothing(self, vector_store, corpus_cards):
        _sync(vector_store, corpus_cards)
        report = _sync(vector_store, corpus_cards)
        assert report.embedded == 0
        assert report.unchanged == 35

    def test_a_wiped_index_is_rebuilt_even_though_the_registry_is_settled(
        self, vector_store, corpus_cards
    ):
        """The failure decision-driven embedding cannot recover from.

        The registry says every fact is UNCHANGED, so `cards_to_embed()` is
        empty. If sync trusted that, the index would stay empty forever.
        """
        _sync(vector_store, corpus_cards)
        vector_store.drop()

        report = _sync(vector_store, corpus_cards, decided_to_embed=set())
        assert report.embedded == 35
        assert report.repaired == 35
        assert vector_store.count() == 35

    def test_a_single_missing_chunk_is_repaired(self, vector_store, corpus_cards):
        _sync(vector_store, corpus_cards)
        vector_store.delete(["mo_elss:nav#0"])

        report = _sync(vector_store, corpus_cards, decided_to_embed=set())
        assert report.embedded == 1
        assert report.repaired == 1
        assert report.repaired_ids == ["mo_elss:nav#0"]
        assert vector_store.count() == 35

    def test_expected_embeds_are_not_counted_as_repairs(self, vector_store, corpus_cards):
        report = _sync(vector_store, corpus_cards, decided_to_embed=_doc_ids(corpus_cards))
        assert report.embedded == 35
        assert report.repaired == 0

    def test_repair_accounting_is_skipped_for_a_rebuild(self, vector_store, corpus_cards):
        report = _sync(vector_store, corpus_cards, decided_to_embed=None)
        assert report.embedded == 35
        assert report.repaired == 0


class TestSyncDecidesEmbedVersusRestamp:
    def test_changed_text_re_embeds(self, vector_store, corpus_cards):
        _sync(vector_store, corpus_cards)
        moved = [
            dataclasses.replace(c, text=c.text + " (revised)") if c.doc_id == "mo_elss:nav" else c
            for c in corpus_cards
        ]
        report = _sync(vector_store, moved)
        assert report.embedded == 1
        assert report.unchanged == 34

    def test_a_new_date_alone_restamps_and_does_not_re_embed(self, vector_store, corpus_cards):
        """P2.7's REFRESHED outcome — what makes the daily cadence cheap."""
        _sync(vector_store, corpus_cards)
        redated = [
            dataclasses.replace(c, source_as_of="2026-09-01") if c.doc_type == "min_sip" else c
            for c in corpus_cards
        ]
        report = _sync(vector_store, redated)
        assert report.embedded == 0
        assert report.restamped == 5
        assert report.unchanged == 30
        state = vector_store.indexed_state()
        assert state["mo_elss:min_sip#0"]["source_as_of"] == "2026-09-01"


class TestSyncLeavesQuarantinedFactsAlone:
    """I-09: the registry kept the old value, so the index keeps the old text."""

    def test_a_conflicted_fact_is_not_written(self, vector_store, corpus_cards):
        _sync(vector_store, corpus_cards)
        original = vector_store.indexed_state()["mo_elss:expense_ratio#0"]

        tampered = [
            dataclasses.replace(c, text="The TER of the ELSS fund is 9.99% per annum.")
            if c.doc_id == "mo_elss:expense_ratio"
            else c
            for c in corpus_cards
        ]
        report = _sync(vector_store, tampered, conflicted_doc_ids={"mo_elss:expense_ratio"})

        assert report.embedded == 0
        assert report.skipped_conflicts == 1
        assert vector_store.indexed_state()["mo_elss:expense_ratio#0"] == original

    def test_a_conflicted_fact_is_not_deleted(self, vector_store, corpus_cards):
        _sync(vector_store, corpus_cards)
        report = _sync(
            vector_store,
            [c for c in corpus_cards if c.doc_id != "mo_elss:expense_ratio"],
            conflicted_doc_ids={"mo_elss:expense_ratio"},
            registry_doc_ids=_doc_ids(corpus_cards) - {"mo_elss:expense_ratio"},
        )
        assert report.deleted == 0
        assert vector_store.count() == 35


class TestSyncDeletion:
    def test_deletes_only_what_the_registry_no_longer_holds(self, vector_store, corpus_cards):
        _sync(vector_store, corpus_cards)
        kept = [c for c in corpus_cards if c.doc_id != "mo_elss:benchmark"]
        report = _sync(vector_store, kept, registry_doc_ids=_doc_ids(kept))
        assert report.deleted == 1
        assert "mo_elss:benchmark#0" not in vector_store.indexed_state()

    def test_a_fact_the_page_stopped_yielding_keeps_its_chunk(self, vector_store, corpus_cards):
        """P2.8 keeps the registry row ageing; the index must age with it."""
        _sync(vector_store, corpus_cards)
        report = _sync(
            vector_store,
            [c for c in corpus_cards if c.doc_id != "mo_elss:benchmark"],
            registry_doc_ids=_doc_ids(corpus_cards),  # registry still holds it
        )
        assert report.deleted == 0
        assert vector_store.count() == 35

    def test_a_single_scheme_run_never_deletes_the_others(self, vector_store, corpus_cards):
        """--scheme mo_elss must not erase the other four."""
        _sync(vector_store, corpus_cards)
        elss = [c for c in corpus_cards if c.scheme_id == "mo_elss"]
        report = vector_store.sync(elss, scope=["mo_elss"], registry_doc_ids=_doc_ids(elss))
        assert report.deleted == 0
        assert vector_store.count() == 35
        assert report.unchanged == 7


class TestRetrievedChunk:
    def test_similarity_inverts_cosine_distance(self):
        chunk = RetrievedChunk.from_chroma(
            "d#0",
            "text",
            {
                "doc_id": "d",
                "scheme_id": "s",
                "doc_type": "nav",
                "source_url": "u",
                "source_as_of": "2026-08-28",
            },
            0.25,
        )
        assert chunk.similarity == pytest.approx(0.75)

    def test_identical_text_scores_one(self, populated_store, corpus_cards, fake_embedder):
        """Queried with the chunk's *embedded* text — what the vector was built from."""
        chunk = next(c for c in chunk_cards(corpus_cards) if c.doc_id == "mo_elss:nav")
        hits = populated_store.query(
            fake_embedder.encode_query(chunk.embed_text), scheme_id="mo_elss", top_k=1
        )
        assert hits[0].similarity == pytest.approx(1.0, abs=1e-6)


class TestSyncReport:
    def test_as_dict_keys_are_stable(self):
        assert set(SyncReport().as_dict()) == {
            "embedded",
            "restamped",
            "deleted",
            "unchanged",
            "skipped_conflicts",
            "repaired",
        }

    def test_written_totals_writes(self):
        assert SyncReport(embedded=3, restamped=2).written == 5
