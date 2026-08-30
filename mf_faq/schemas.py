"""Pydantic models for the YAML configs.

These exist so a malformed config fails LOUDLY AT BOOT rather than silently at
23:30 during the scheduled run (P1 exit criterion). Every validator here earns
its place by catching a specific failure that would otherwise be discovered in
production — see the error messages for what each one prevents.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class StalePolicy(StrEnum):
    """What to do when a fact is older than its max_age (PS §8.6)."""

    REFUSE = "refuse"
    FLAG = "flag"
    EXEMPT = "exempt"


class DocTypeConfig(BaseModel):
    """Freshness policy for one fact type."""

    model_config = ConfigDict(extra="forbid")

    max_age_days: int | None = Field(default=None, ge=0)
    unit: Literal["days", "business_days"] = "days"
    past_max_age: StalePolicy
    date_field: str
    date_is_page_level: bool = False

    @model_validator(mode="after")
    def _check_age_policy_coherent(self) -> Self:
        # A gate that can never fire is almost always a config mistake. The one
        # legitimate case is the page-level-date facts (PS §9), which are
        # deliberately exempt because nav_date advances daily.
        if self.past_max_age is StalePolicy.EXEMPT:
            if self.max_age_days is not None:
                raise ValueError(
                    "past_max_age='exempt' with a max_age_days set is contradictory: "
                    "the gate would never fire. Set max_age_days: null."
                )
        elif self.max_age_days is None:
            raise ValueError(
                f"past_max_age='{self.past_max_age}' requires max_age_days. "
                "Without it the freshness gate cannot evaluate this doc_type."
            )
        return self


class FactSourceConfig(BaseModel):
    """Crawl policy for the single permitted fact domain (ARCH §12)."""

    model_config = ConfigDict(extra="forbid")

    domain: str
    parser: str
    crawl_delay_seconds: float = Field(ge=0)
    user_agent: str
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)

    @field_validator("user_agent")
    @classmethod
    def _must_identify(cls, v: str) -> str:
        # PS §4.5 behaviour 8 requires an identifying agent. A default or empty
        # UA against a regulated public site every day is not acceptable.
        if len(v.strip()) < 10:
            raise ValueError("user_agent must be a real identifying string (PS §4.5 behaviour 8)")
        return v


class OutOfCorpusFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str
    reason: str = Field(min_length=10)  # a bare exclusion with no reason is not reviewable


class SchemeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheme_id: str = Field(pattern=r"^[a-z0-9_]+$")
    display_name: str
    category: str
    url: HttpUrl
    aliases: list[str] = Field(default_factory=list)
    extract: list[str] = Field(min_length=1)


class SourcesConfig(BaseModel):
    """The corpus definition (`config/sources.yaml`)."""

    model_config = ConfigDict(extra="forbid")

    version: int
    fact_source: FactSourceConfig
    doc_types: dict[str, DocTypeConfig]
    out_of_corpus: list[OutOfCorpusFact] = Field(default_factory=list)
    schemes: list[SchemeConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_corpus(self) -> Self:
        # 1. Every extracted fact must have a freshness policy, or the Phase 4
        #    gate would hit a KeyError at query time on a real user question.
        known = set(self.doc_types)
        for scheme in self.schemes:
            unknown = set(scheme.extract) - known
            if unknown:
                raise ValueError(
                    f"scheme '{scheme.scheme_id}' extracts {sorted(unknown)} "
                    f"which have no doc_types entry. Known: {sorted(known)}"
                )

        # 2. scheme_id must be unique — duplicates would silently overwrite each
        #    other in the registry and index.
        ids = [s.scheme_id for s in self.schemes]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate scheme_id: {dupes}")

        # 3. Every fact URL must be on the permitted domain. This is the
        #    code-level guarantee behind PS §5.1 — a stray AMC or AMFI URL here
        #    would quietly reintroduce the fallback corpus that §8.1 removed.
        allowed = self.fact_source.domain
        for scheme in self.schemes:
            host = (scheme.url.host or "").removeprefix("www.")
            if host != allowed:
                raise ValueError(
                    f"scheme '{scheme.scheme_id}' url host '{host}' is not the "
                    f"permitted fact domain '{allowed}' (PS §5.1, ARCH §12)"
                )

        # 4. An out_of_corpus fact must not also be extracted — a contradiction
        #    that would let a supposedly-refused fact reach the index.
        excluded = {f.fact for f in self.out_of_corpus}
        for scheme in self.schemes:
            leaked = set(scheme.extract) & excluded
            if leaked:
                raise ValueError(
                    f"scheme '{scheme.scheme_id}' extracts {sorted(leaked)} "
                    "which is declared out_of_corpus"
                )

        # 5. Aliases must not collide across schemes. Two schemes answering to
        #    the same alias is the cross-scheme misattribution this design works
        #    hardest to prevent (ARCH §6.1).
        seen: dict[str, str] = {}
        for scheme in self.schemes:
            for alias in [scheme.display_name, *scheme.aliases]:
                key = alias.strip().casefold()
                if key in seen and seen[key] != scheme.scheme_id:
                    raise ValueError(
                        f"alias '{alias}' maps to both '{seen[key]}' and "
                        f"'{scheme.scheme_id}' — would cause wrong-scheme answers"
                    )
                seen[key] = scheme.scheme_id
        return self

    def scheme(self, scheme_id: str) -> SchemeConfig:
        for s in self.schemes:
            if s.scheme_id == scheme_id:
                return s
        raise KeyError(f"unknown scheme_id: {scheme_id}")


# --------------------------------------------------------------------------
# Refusal links (config/refusal_links.yaml)
# --------------------------------------------------------------------------


class LinkTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    label: str


class RefusalLinksConfig(BaseModel):
    """Refusal link registry, routed by refusal reason (P0.6)."""

    model_config = ConfigDict(extra="forbid")

    link_health_domains: list[str]
    factsheet_links: dict[str, LinkTarget]
    factsheet_fallback: LinkTarget
    educational_links: dict[str, LinkTarget]
    reason_routing: dict[str, Literal["factsheet", "educational", "none"]]

    @model_validator(mode="after")
    def _validate_routing(self) -> Self:
        if "default" not in self.educational_links:
            raise ValueError("educational_links must define a 'default' entry")

        # The link-health domains must cover every URL we will HEAD-check.
        # A URL outside them would be blocked by the fetcher at runtime, so the
        # daily check would report a dead link that is actually fine.
        domains = {d.removeprefix("www.") for d in self.link_health_domains}
        targets = [
            *self.factsheet_links.values(),
            self.factsheet_fallback,
            *self.educational_links.values(),
        ]
        for t in targets:
            host = (t.url.host or "").removeprefix("www.")
            if host not in domains:
                raise ValueError(
                    f"refusal link host '{host}' is not in link_health_domains "
                    f"{sorted(domains)} — the liveness checker could not reach it"
                )
        return self

    def link_for(self, reason: str, scheme_id: str | None = None) -> LinkTarget | None:
        """Resolve the outbound link for a refusal reason.

        Returns None for reasons that must NOT carry an outbound link (pii,
        ambiguous_scheme). Unknown reasons fail closed to educational rather
        than raising — a refusal must always render.
        """
        route = self.reason_routing.get(reason, "educational")
        if route == "none":
            return None
        if route == "factsheet":
            if scheme_id and scheme_id in self.factsheet_links:
                return self.factsheet_links[scheme_id]
            return self.factsheet_fallback
        return self.educational_links["default"]
