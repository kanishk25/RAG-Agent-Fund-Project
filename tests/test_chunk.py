"""Chunking and metadata stamping (P3.1, P3.2)."""

from __future__ import annotations

from mf_faq.ingest.fact_card import FactCard
from mf_faq.retrieval.chunk import (
    Chunk,
    chunk_card,
    chunk_cards,
    chunk_id_for,
    strip_scheme_name,
)

NAMES = ["Motilal Oswal ELSS Tax Saver Fund Direct Growth", "elss"]
NAV_TEXT = f"The latest declared NAV of {NAMES[0]} is 41.7 per unit."

REQUIRED_METADATA = {
    "doc_id",
    "scheme_id",
    "doc_type",
    "source_url",
    "source_as_of",
    "chunk_index",
    "date_is_page_level",
    "card_hash",
    "embed_hash",
}


def _card(**over) -> FactCard:
    defaults = dict(
        doc_id="mo_elss:nav",
        scheme_id="mo_elss",
        doc_type="nav",
        text="The latest declared NAV of X is ₹1.23 per unit.",
        value_hash="v0",
        source_url="https://groww.in/mutual-funds/x",
        source_as_of="2026-08-28",
        date_is_page_level=False,
    )
    return FactCard(**{**defaults, **over})


def _chunk(**over):
    return chunk_card(_card(**over), NAMES)


class TestOneCardOneChunk:
    """ARCH §6.1 mitigation 2 — single-attribute chunks, guaranteed upstream."""

    def test_each_card_yields_exactly_one_chunk(self, corpus_cards):
        assert len(chunk_cards(corpus_cards)) == len(corpus_cards)

    def test_holdings_are_not_split(self, corpus_cards):
        """R-08: a holdings list is one chunk or `top_k=4` truncates the answer."""
        holdings = [c for c in corpus_cards if c.doc_type == "holdings"]
        assert len(holdings) == 5
        chunks = chunk_cards(holdings)
        assert len(chunks) == 5
        for card, chunk in zip(holdings, chunks, strict=True):
            assert chunk.text == card.text  # verbatim, not summarised or trimmed

    def test_chunk_ids_are_unique_across_the_corpus(self, corpus_cards):
        ids = [c.chunk_id for c in chunk_cards(corpus_cards)]
        assert len(set(ids)) == len(ids) == 35


class TestMetadataStamping:
    """P3.2 — every field is read by a later stage; none is decorative."""

    def test_metadata_carries_every_required_field(self):
        assert set(_chunk().metadata()) == REQUIRED_METADATA

    def test_metadata_values_come_from_the_card(self):
        meta = _chunk().metadata()
        assert meta["doc_id"] == "mo_elss:nav"
        assert meta["scheme_id"] == "mo_elss"
        assert meta["doc_type"] == "nav"
        assert meta["source_url"] == "https://groww.in/mutual-funds/x"
        assert meta["source_as_of"] == "2026-08-28"
        assert meta["chunk_index"] == 0

    def test_metadata_is_flat_scalars_only(self, corpus_cards):
        """Chroma rejects nested metadata; a list would fail at write time."""
        for chunk in chunk_cards(corpus_cards):
            for key, value in chunk.metadata().items():
                assert isinstance(value, str | int | float | bool), (key, type(value))

    def test_card_hash_is_the_served_text_hash(self):
        card = _card()
        assert _chunk().card_hash == card.text_hash

    def test_card_hash_tracks_text_not_value(self):
        """The sync compares this to decide rewriting — it must follow text."""
        a = _chunk(text="one", value_hash="same")
        b = _chunk(text="two", value_hash="same")
        assert a.card_hash != b.card_hash

    def test_source_as_of_reaches_metadata_but_never_the_text(self, corpus_cards):
        """P2.6's rule, still true one layer down: the date is metadata only."""
        for chunk in chunk_cards(corpus_cards):
            assert chunk.source_as_of
            assert chunk.source_as_of not in chunk.text


class TestEmbedText:
    """The measured split: scheme name out of the vector, never out of the text."""

    def test_the_scheme_name_is_stripped_from_the_embedded_text(self):
        chunk = _chunk(text=NAV_TEXT)
        assert "Motilal Oswal" not in chunk.embed_text
        assert "41.7" in chunk.embed_text

    def test_the_served_text_keeps_the_scheme_name(self):
        chunk = _chunk(text=NAV_TEXT)
        assert "Motilal Oswal ELSS Tax Saver Fund Direct Growth" in chunk.text

    def test_longest_name_is_stripped_first(self):
        """Otherwise the alias 'elss' eats a fragment and leaves the rest behind."""
        assert "Tax Saver" not in strip_scheme_name(
            "NAV of Motilal Oswal ELSS Tax Saver Fund Direct Growth is 1.", NAMES
        )

    def test_falls_back_to_the_full_card_when_nothing_would_survive(self):
        chunk = _chunk(text="Motilal Oswal ELSS Tax Saver Fund Direct Growth")
        assert chunk.embed_text == chunk.text

    def test_every_real_card_keeps_its_value_after_stripping(self, corpus_cards):
        """Stripping must never eat the fact itself."""
        for card, chunk in zip(corpus_cards, chunk_cards(corpus_cards), strict=True):
            assert len(chunk.embed_text) >= 8
            assert chunk.embed_text != card.text  # something was removed
            assert len(chunk.embed_text) < len(card.text)

    def test_embed_hash_tracks_the_embedded_text(self):
        a = chunk_card(_card(text=NAV_TEXT), NAMES)
        b = chunk_card(_card(text=NAV_TEXT), ["Some Other Fund"])
        assert a.text == b.text
        assert a.card_hash == b.card_hash  # same served card
        assert a.embed_hash != b.embed_hash  # different vector

    def test_embed_hash_is_in_metadata(self):
        assert _chunk().metadata()["embed_hash"] == _chunk().embed_hash


class TestChunkId:
    def test_format(self):
        assert chunk_id_for("mo_elss:nav") == "mo_elss:nav#0"
        assert chunk_id_for("mo_elss:nav", 3) == "mo_elss:nav#3"

    def test_chunk_id_derives_from_doc_id(self):
        chunk = _chunk()
        assert chunk.chunk_id == chunk_id_for(chunk.doc_id, chunk.chunk_index)

    def test_chunk_is_frozen(self):
        import dataclasses

        import pytest

        with pytest.raises(dataclasses.FrozenInstanceError):
            _chunk().text = "mutated"  # type: ignore[misc]

    def test_chunk_type(self, corpus_cards):
        assert all(isinstance(c, Chunk) for c in chunk_cards(corpus_cards))
