"""Query text → `scheme_id`, or an honest refusal to guess (P3.5).

This is the gate in front of ARCH §6.1 mitigation 1. The Chroma filter
`where={"scheme_id": ...}` makes cross-scheme leakage structurally impossible —
*given the right `scheme_id`*. Resolving the wrong one does not trip any later
guardrail: the answer is fluent, correctly cited, correctly dated, and about the
wrong fund. So this module's job is not merely to resolve well; it is to **fail
loudly rather than plausibly**.

Why token intersection, not a similarity score
----------------------------------------------
The obvious approach is to score each scheme's name against the query and take
the best. It is the wrong shape here, and "Motilal Oswal BSE index fund" shows
why: it is a genuinely ambiguous query naming two real schemes. Any scoring
function still returns a *ranking*, and BSE Enhanced Value happens to outscore
BSE Financials ex Bank 30 for incidental reasons (fewer unmatched name tokens).
A threshold-plus-margin rule then confidently picks one. The user asked a
question with two answers and got one, silently.

So the decision is set-based instead:

  1. Tokenise every scheme's `display_name` and `aliases` into a vocabulary.
  2. Drop tokens that appear in **every** scheme ("motilal", "oswal", "fund",
     "direct", "growth"). They are shared branding and carry no information —
     computed by document frequency, never hardcoded, so adding a sixth scheme
     re-derives it rather than rotting.
  3. Match query tokens against that vocabulary, exactly or by close fuzz.
  4. **Intersect** the scheme sets of every matched token. One scheme surviving
     means the query's distinctive words are consistent with exactly one fund.

The intersection *is* the ambiguity detector, not a separate heuristic bolted on
after. "BSE index fund" matches `bse` → {value, financials} and `index` →
{value, next50, financials}; the intersection is two schemes, so the answer is
"two", and no tuning constant can turn it into one. That is the property worth
having (edge cases Q-07, Q-08).

Fuzzy matching, and where it is switched off
--------------------------------------------
Q-09 requires "Motilul Oswal larg and midcap" to resolve. Query tokens therefore
match vocabulary tokens by `difflib` ratio ≥ 0.82 — enough for larg→large
(0.89), well short of a coincidence.

Fuzz is disabled for tokens shorter than 4 characters, where the ratio stops
meaning anything: `50` vs `30` scores 0.5, and the two BSE index funds are told
apart partly by exactly those digits. Short tokens must match exactly.

`STOPWORDS` — the trap that made this necessary
-----------------------------------------------
Document frequency alone is not enough, and it fails in the direction that
matters. A word appearing in exactly one scheme's name is, to DF, maximally
distinctive — even when it is a word nobody means as a fund name:

  "what is the expense ratio **and** exit load?"  → *Large **and** Midcap*
  "what is the net asset **value**?"              → *Enhanced **Value** Index Fund*
  "**Axis Long Term** Equity Fund"                → *Most Focused **Long Term** Fund*

Each resolves with total confidence to the wrong fund, and the last one answers
a question about a fund we do not hold using one we do. Two word lists are
therefore stripped before DF is computed at all: ordinary English
(`ENGLISH_STOPWORDS`) and mutual-fund fact vocabulary (`DOMAIN_TERMS`). Both are
kept small and separately named, because over-stripping eats real name tokens —
the trade-off for each domain term is recorded beside it.

Known limit, stated rather than hidden
--------------------------------------
An out-of-corpus fund ("NAV of HDFC Top 100?") contains no vocabulary token and
comes back `NO_SCHEME` — the same outcome as naming no fund at all (Q-07/Q-10).
Both refuse and neither guesses, so retrieval is correct either way, but the two
deserve different wording. Telling them apart means recognising fund names we
deliberately do not hold, which belongs to the P4 intent layer, not here.
"""

from __future__ import annotations

import functools
import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from mf_faq.schemas import SourcesConfig
from mf_faq.settings import get_sources

#: difflib ratio at which two tokens are considered the same word.
#: larg→large is 0.89; 50→30 is 0.50. The gap is wide, so the exact value is
#: not delicate — but it is a tuning point, so it is named.
FUZZY_THRESHOLD = 0.82

#: Below this length a token must match exactly. See the module docstring.
MIN_FUZZY_LENGTH = 4

#: Below this, `strip_scheme_terms` returns the query untouched rather than
#: embedding a near-empty string. See that method.
MIN_STRIPPED_QUERY_CHARS = 3

#: A resolution needs at least one matched distinctive token of this length.
#: Stops a stray two-character token ("ex", from *Financials ex Bank 30*) from
#: single-handedly resolving a scheme.
MIN_EVIDENCE_LENGTH = 3

#: A single matched token is not enough to RESOLVE (as opposed to clarify) via
#: the token-intersection path. See the P4 finding beside its use: "Motilal
#: Oswal Midcap Fund" — a fund outside the corpus — matched only "midcap" and
#: resolved with full confidence to mo_large_midcap before this was added.
MIN_TOKENS_TO_RESOLVE = 2

#: Ordinary English. Removed before document frequency is computed — see the
#: docstring for why "and" alone made this necessary.
ENGLISH_STOPWORDS = frozenset(
    """
    a an the and or of for from with in on at by to is are was were be been am
    what which who whom whose how why when where much many does do did can could
    should would will shall may might must i me my we us our you your it its
    this that these those there here as if then than so such please tell show
    give about into over under between during any all some no not
    """.split()  # noqa: SIM905 - a readable block beats a 78-element literal
)

#: Words that belong to the *fact* vocabulary, not the *fund-name* vocabulary.
#:
#: This list is not defensive tidying — it was written against two live
#: wrong-scheme resolutions the tests caught:
#:
#:   "net asset value"            → mo_bse_value, via *Enhanced **Value** Index Fund*
#:   "Axis Long Term Equity Fund" → mo_elss,      via *Most Focused **Long Term** Fund*
#:
#: The second is the worse one: a question about a fund we do not cover answered
#: with a fund we do. Both share a shape — a common domain phrase overlapping a
#: fund's name — and neither is caught by document frequency, because the word
#: genuinely does appear in exactly one scheme.
#:
#: Every removal here costs recall, so each is checked against the alternative
#: discriminator that survives it:
#:   value      → *Enhanced* still identifies mo_bse_value
#:   long, term → *focused* and the alias *elss* still identify mo_elss
#:
#: `tax` and `saver` are deliberately NOT here. They collide with tax questions,
#: but they are the only tokens that resolve "Motilal Oswal Tax Saver Fund" when
#: the asker omits "ELSS", and losing that costs more than the collision does —
#: a tax question with no fund named still refuses on its own merits in P4.
#:
#: The rest appear in no scheme name today. They are listed anyway so that a
#: sixth scheme called "... Growth Value Fund" cannot quietly make "value" a
#: discriminator again.
DOMAIN_TERMS = frozenset(
    """
    net asset value long term nav expense ratio ter exit load lock in minimum
    min sip benchmark holding holdings portfolio company companies weight
    return returns performance price unit units amount date plan option scheme
    schemes mutual invest investment investing investor risk riskometer
    """.split()  # noqa: SIM905 - a readable block beats a 40-element literal
)

#: What the tokeniser strips. Kept as two named halves so the reason for any one
#: word is visible: English noise above, domain collisions below.
STOPWORDS = ENGLISH_STOPWORDS | DOMAIN_TERMS

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Resolution(StrEnum):
    """Outcome of resolving a query to a scheme."""

    RESOLVED = "resolved"
    #: Two or more schemes fit. P4 must clarify, never pick (Q-08).
    AMBIGUOUS = "matches_multiple_schemes"
    #: Nothing distinctive matched: no fund named, or one we do not hold (Q-07/Q-10).
    NO_SCHEME = "no_scheme_named"


@dataclass(frozen=True)
class SchemeMatch:
    """A resolution, with the evidence that produced it.

    `matched_tokens` exists so a wrong resolution can be explained rather than
    merely observed — it is the difference between "the resolver picked the
    wrong fund" and "the resolver matched on the word 'and'".
    """

    outcome: Resolution
    scheme_id: str | None
    candidates: tuple[str, ...]
    matched_tokens: tuple[str, ...]
    reason: str

    @property
    def resolved(self) -> bool:
        return self.outcome is Resolution.RESOLVED


def tokenise(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords removed."""
    return [t for t in _TOKEN_RE.findall(text.casefold()) if t not in STOPWORDS]


def _normalise_phrase(text: str) -> str:
    return " ".join(tokenise(text))


class SchemeResolver:
    """Resolves query text against one `sources.yaml`.

    Built once and reused: the vocabulary and document frequencies are derived
    from config at construction, so resolution itself is pure dictionary work.
    """

    def __init__(self, sources: SourcesConfig):
        self.sources = sources
        self._scheme_ids = [s.scheme_id for s in sources.schemes]

        # token -> {scheme_id}, over display_name + aliases.
        vocab: dict[str, set[str]] = defaultdict(set)
        # Full alias phrases, for the exact-phrase fast path.
        phrases: dict[str, str] = {}
        for scheme in sources.schemes:
            for name in [scheme.display_name, *scheme.aliases]:
                phrase = _normalise_phrase(name)
                if phrase:
                    phrases[phrase] = scheme.scheme_id
                for token in tokenise(name):
                    vocab[token].add(scheme.scheme_id)

        total = len(self._scheme_ids)
        # A token in every scheme's name is shared branding, not a discriminator.
        self.vocabulary = {t: frozenset(ids) for t, ids in vocab.items() if len(ids) < total}
        self.shared_tokens = frozenset(t for t, ids in vocab.items() if len(ids) == total)
        self.phrases = phrases

    # -- matching ----------------------------------------------------------

    def _match_token(self, token: str) -> str | None:
        """Best vocabulary token for one query token, or None."""
        if token in self.vocabulary:
            return token
        if len(token) < MIN_FUZZY_LENGTH:
            return None  # fuzz is meaningless at this length — see docstring
        best, best_ratio = None, 0.0
        for candidate in self.vocabulary:
            if len(candidate) < MIN_FUZZY_LENGTH:
                continue
            ratio = SequenceMatcher(None, token, candidate).ratio()
            if ratio > best_ratio:
                best, best_ratio = candidate, ratio
        return best if best_ratio >= FUZZY_THRESHOLD else None

    def _phrase_hits(self, query: str) -> set[str]:
        """Schemes whose full display name or alias appears verbatim in the query.

        The fast path for the renamed ELSS fund (P0.2): "Most Focused Long Term
        Fund" is an alias, so it resolves as a phrase without needing every one
        of its tokens to be individually distinctive.
        """
        normalised = _normalise_phrase(query)
        return {
            scheme_id
            for phrase, scheme_id in self.phrases.items()
            if phrase and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", normalised)
        }

    def resolve(self, query: str) -> SchemeMatch:
        """Resolve `query` to one scheme, several, or none."""
        hits = self._phrase_hits(query)
        if len(hits) == 1:
            scheme_id = next(iter(hits))
            return SchemeMatch(
                outcome=Resolution.RESOLVED,
                scheme_id=scheme_id,
                candidates=(scheme_id,),
                matched_tokens=(),
                reason="query contains a full scheme name or alias",
            )

        matched: dict[str, frozenset[str]] = {}
        for token in tokenise(query):
            vocab_token = self._match_token(token)
            if vocab_token is not None:
                matched[vocab_token] = self.vocabulary[vocab_token]

        if not matched:
            return SchemeMatch(
                outcome=Resolution.NO_SCHEME,
                scheme_id=None,
                candidates=(),
                matched_tokens=(),
                reason="no distinctive scheme token in the query",
            )

        tokens = tuple(sorted(matched))
        if not any(len(t) >= MIN_EVIDENCE_LENGTH for t in tokens):
            # Matched only on scraps ("ex", "50"). Not enough to name a fund.
            return SchemeMatch(
                outcome=Resolution.NO_SCHEME,
                scheme_id=None,
                candidates=(),
                matched_tokens=tokens,
                reason="matched only tokens too short to identify a scheme",
            )

        intersection: set[str] = set(self._scheme_ids)
        for ids in matched.values():
            intersection &= ids

        if len(intersection) == 1:
            scheme_id = next(iter(intersection))
            if len(tokens) < MIN_TOKENS_TO_RESOLVE:
                # ⚠ Found live: "Motilal Oswal Midcap Fund" — a fund this corpus
                # does NOT hold — matched only the single word "midcap" (split out
                # of "Large and Midcap Fund") and resolved with full confidence to
                # mo_large_midcap. One word carved out of a longer name is not the
                # same evidence as a curated alias: "elss" and "next 50" resolve
                # confidently because they were deliberately declared as aliases
                # in sources.yaml (the phrase path above), not because a single
                # token happened to be unique. A single incidental token gets the
                # same treatment as scraps below the length floor: not enough to
                # name a fund, so this refuses to guess (Q-10) rather than pass
                # this bar wrongly. Tightening this cost nothing measured against
                # the golden set — every golden resolution goes through the
                # phrase path and carries zero matched tokens.
                return SchemeMatch(
                    outcome=Resolution.NO_SCHEME,
                    scheme_id=None,
                    candidates=(),
                    matched_tokens=tokens,
                    reason=(
                        f"token {list(tokens)} alone fits only {scheme_id}, but a single "
                        "incidental word is not enough evidence — could name a different, "
                        "uncovered fund that happens to share it"
                    ),
                )
            return SchemeMatch(
                outcome=Resolution.RESOLVED,
                scheme_id=scheme_id,
                candidates=(scheme_id,),
                matched_tokens=tokens,
                reason=f"distinctive tokens {list(tokens)} fit only this scheme",
            )

        if intersection:
            candidates = self._ordered(intersection)
            return SchemeMatch(
                outcome=Resolution.AMBIGUOUS,
                scheme_id=None,
                candidates=candidates,
                matched_tokens=tokens,
                reason=f"distinctive tokens {list(tokens)} fit {len(candidates)} schemes",
            )

        # No single scheme holds every matched token — the query names words from
        # more than one fund ("large midcap and elss"). Still ambiguous, but the
        # candidate list is the union: every scheme the query touched.
        union: set[str] = set()
        for ids in matched.values():
            union |= ids
        candidates = self._ordered(union)
        return SchemeMatch(
            outcome=Resolution.AMBIGUOUS,
            scheme_id=None,
            candidates=candidates,
            matched_tokens=tokens,
            reason=f"tokens {list(tokens)} span {len(candidates)} schemes; none holds them all",
        )

    def strip_scheme_terms(self, query: str, scheme_id: str) -> str:
        """Remove the fund's own name from a query before it is embedded.

        The mirror of `chunk.strip_scheme_name`, and it exists for the same
        measured reason: the scheme is already pinned by the metadata filter, so
        its name contributes nothing to picking *which fact* the user wants — it
        only crowds the vector. Measured over the golden set, stripping the
        query as well as the card lifts recall@1 from 94.1% to 97.1%.

        Token-wise and fuzzy, unlike the card side, because a user's spelling is
        not ours to assume: "Motilul Oswal larg and midcap" must still be
        recognised as the fund's name and removed (Q-09). Punctuation and the
        casing of surviving words are preserved — only matched words are cut.

        Falls back to the original query when too little survives, which is what
        a query consisting of nothing *but* a fund name does. Embedding an empty
        string would be worse than embedding a redundant one.
        """
        scheme_tokens = {t for t, ids in self.vocabulary.items() if scheme_id in ids}
        scheme_tokens |= self.shared_tokens

        def cut(match: re.Match) -> str:
            word = match.group(0).casefold()
            if word in scheme_tokens:
                return " "
            if len(word) >= MIN_FUZZY_LENGTH and any(
                len(t) >= MIN_FUZZY_LENGTH
                and SequenceMatcher(None, word, t).ratio() >= FUZZY_THRESHOLD
                for t in scheme_tokens
            ):
                return " "
            return match.group(0)

        stripped = " ".join(re.sub(r"[A-Za-z0-9]+", cut, query).split())
        return stripped if len(stripped) >= MIN_STRIPPED_QUERY_CHARS else query

    def _ordered(self, ids: set[str]) -> tuple[str, ...]:
        """Config order, so clarification prompts are stable run to run."""
        return tuple(s for s in self._scheme_ids if s in ids)


@functools.lru_cache(maxsize=1)
def get_resolver() -> SchemeResolver:
    return SchemeResolver(get_sources())


def resolve_scheme(query: str) -> SchemeMatch:
    return get_resolver().resolve(query)
