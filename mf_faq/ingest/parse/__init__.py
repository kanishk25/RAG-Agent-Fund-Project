"""Fact parsers. Groww is the only fact source (PS §8.1), so there is one."""

from mf_faq.ingest.parse.groww_scheme_page import (
    EXTRACTORS,
    ExtractedFact,
    FactUnavailable,
    SchemeIdentityMismatch,
    parse_facts,
    parse_scheme_page,
)

__all__ = [
    "EXTRACTORS",
    "ExtractedFact",
    "FactUnavailable",
    "SchemeIdentityMismatch",
    "parse_facts",
    "parse_scheme_page",
]
