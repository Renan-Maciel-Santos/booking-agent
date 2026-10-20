"""Deterministic free-text resolution for barbershop services and barbers."""

import difflib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from booking_agent.database.models import Barber, Service

_DEFAULT_SERVICE_ALIASES: dict[str, str] = {
    "corte": "corte de cabelo",
    "cabelo": "corte de cabelo",
    "social": "corte de cabelo",
    "degrade": "corte de cabelo",
    "fade": "corte de cabelo",
    "disfarcado": "corte de cabelo",
    "tesoura": "corte de cabelo",
    "maquina": "corte de cabelo",
    "dar um tapa no visual": "corte de cabelo",
    "barba": "barba",
    "barboterapia": "barba",
    "fazer a barba": "barba",
    "pezinho": "barba",
    "aparar barba": "barba",
    "regua": "barba",
    "cabelo e barba": "corte e barba",
    "barba e cabelo": "corte e barba",
    "combo": "corte e barba",
    "combo completo": "corte e barba",
    "completo": "corte e barba",
    "corte e barba": "corte e barba",
    "corte completo": "corte e barba",
    "sobrancelha": "sobrancelha",
    "designer de sobrancelha": "sobrancelha",
    "fazer a sobrancelha": "sobrancelha",
}

_NO_BARBER_PREFERENCE_TOKENS: set[str] = {
    "sem preferencia",
    "qualquer um",
    "qualquer",
    "tanto faz",
    "qualquer barbeiro",
    "ninguem especifico",
    "indiferente",
    "nenhum",
}


def normalize_text(text: str) -> str:
    """Normalize text by stripping diacritics, punctuation, lowercasing and trimming whitespace."""
    nfkd = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^\w\s]", " ", stripped.lower())
    return " ".join(cleaned.split())


@dataclass(frozen=True)
class EntityResolutionResult[T]:
    """Outcome of attempting to resolve a domain entity from customer free-text."""

    matched: bool
    entity: T | None = None
    candidates: Sequence[T] = ()
    ambiguous: bool = False


class EntityResolver:
    """Resolves free-text service and barber queries against active catalog records."""

    def __init__(self, service_aliases: dict[str, str] | None = None) -> None:
        """Store alias mappings.

        Args:
            service_aliases: Optional custom alias mapping dictionary.
        """
        self._service_aliases = service_aliases or _DEFAULT_SERVICE_ALIASES

    def resolve_service(
        self,
        raw_name: str,
        services: Sequence[Service],
    ) -> EntityResolutionResult[Service]:
        """Resolve a service name from user text using exact, alias, and fuzzy matching.

        Args:
            raw_name: Free-text input provided by customer or extracted by LLM.
            services: Active services available in the catalog.

        Returns:
            EntityResolutionResult with matched Service, candidate list, or ambiguous flag.
        """
        active = [s for s in services if s.active]
        if not active:
            return EntityResolutionResult(matched=False, candidates=())

        norm_input = normalize_text(raw_name)
        if not norm_input:
            return EntityResolutionResult(matched=False, candidates=active)

        # 1. Exact match against normalized service name
        for s in active:
            if normalize_text(s.name) == norm_input:
                return EntityResolutionResult(matched=True, entity=s)

        # 2. Alias mapping lookup
        target_alias = self._service_aliases.get(norm_input)
        if target_alias is not None:
            norm_alias = normalize_text(target_alias)
            for s in active:
                if normalize_text(s.name) == norm_alias:
                    return EntityResolutionResult(matched=True, entity=s)

        # 3. Fuzzy matching with SequenceMatcher (threshold >= 0.80)
        scored: list[tuple[float, Service]] = []
        for s in active:
            norm_svc_name = normalize_text(s.name)
            score = difflib.SequenceMatcher(None, norm_input, norm_svc_name).ratio()

            # Check if input matches any alias for this service
            for alias, canonical in self._service_aliases.items():
                if normalize_text(canonical) == norm_svc_name:
                    alias_score = difflib.SequenceMatcher(None, norm_input, alias).ratio()
                    if alias_score > score:
                        score = alias_score

            if score >= 0.80:
                scored.append((score, s))

        if not scored:
            return EntityResolutionResult(matched=False, candidates=active, ambiguous=False)

        scored.sort(key=lambda item: item[0], reverse=True)

        # Check for ambiguity if top candidates have close scores
        if len(scored) > 1 and (scored[0][0] - scored[1][0] < 0.10):
            return EntityResolutionResult(
                matched=False,
                candidates=[s for _, s in scored[:3]],
                ambiguous=True,
            )

        return EntityResolutionResult(matched=True, entity=scored[0][1])

    def resolve_barber(
        self,
        raw_name: str | None,
        barbers: Sequence[Barber],
    ) -> EntityResolutionResult[Barber]:
        """Resolve a barber from user text using exact, prefix, and fuzzy matching.

        Args:
            raw_name: Free-text barber name or None for no preference.
            barbers: Active barbers available in the catalog.

        Returns:
            EntityResolutionResult with matched Barber, None for 'any barber', or candidates.
        """
        active = [b for b in barbers if b.active]
        if not active:
            return EntityResolutionResult(matched=False, candidates=())

        if raw_name is None:
            # Explicit None means any barber
            return EntityResolutionResult(matched=True, entity=None)

        norm_input = normalize_text(raw_name)
        if not norm_input or norm_input in _NO_BARBER_PREFERENCE_TOKENS:
            # Customer explicitly expressed no preference
            return EntityResolutionResult(matched=True, entity=None)

        # 1. Exact match against full name
        exact_matches = [b for b in active if normalize_text(b.name) == norm_input]
        if len(exact_matches) == 1:
            return EntityResolutionResult(matched=True, entity=exact_matches[0])
        if len(exact_matches) > 1:
            return EntityResolutionResult(matched=False, candidates=exact_matches, ambiguous=True)

        # 2. First name match
        first_name_matches = [b for b in active if normalize_text(b.name).split()[0] == norm_input]
        if len(first_name_matches) == 1:
            return EntityResolutionResult(matched=True, entity=first_name_matches[0])
        if len(first_name_matches) > 1:
            return EntityResolutionResult(
                matched=False, candidates=first_name_matches, ambiguous=True
            )

        # 3. Prefix match
        prefix_matches = [b for b in active if normalize_text(b.name).startswith(norm_input)]
        if len(prefix_matches) == 1:
            return EntityResolutionResult(matched=True, entity=prefix_matches[0])
        if len(prefix_matches) > 1:
            return EntityResolutionResult(matched=False, candidates=prefix_matches, ambiguous=True)

        # 4. Fuzzy matching (threshold >= 0.80)
        scored: list[tuple[float, Barber]] = []
        for b in active:
            norm_b_name = normalize_text(b.name)
            first_name = norm_b_name.split()[0]
            score = max(
                difflib.SequenceMatcher(None, norm_input, norm_b_name).ratio(),
                difflib.SequenceMatcher(None, norm_input, first_name).ratio(),
            )
            if score >= 0.80:
                scored.append((score, b))

        if not scored:
            return EntityResolutionResult(matched=False, candidates=active, ambiguous=False)

        scored.sort(key=lambda item: item[0], reverse=True)

        if len(scored) > 1 and (scored[0][0] - scored[1][0] < 0.10):
            return EntityResolutionResult(
                matched=False,
                candidates=[b for _, b in scored[:3]],
                ambiguous=True,
            )

        return EntityResolutionResult(matched=True, entity=scored[0][1])
