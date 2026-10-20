"""Unit tests for EntityResolver service and barber free-text matching."""

import uuid

from booking_agent.database.models import Barber, Service
from booking_agent.services.entity_resolver import EntityResolver, normalize_text


def _make_services() -> list[Service]:
    """Create sample active services for resolution tests."""
    return [
        Service(
            id=uuid.uuid4(),
            name="Corte de Cabelo",
            duration_minutes=30,
            price_cents=3500,
            active=True,
        ),
        Service(id=uuid.uuid4(), name="Barba", duration_minutes=30, price_cents=3000, active=True),
        Service(
            id=uuid.uuid4(),
            name="Corte e Barba",
            duration_minutes=60,
            price_cents=6000,
            active=True,
        ),
        Service(
            id=uuid.uuid4(), name="Sobrancelha", duration_minutes=15, price_cents=1500, active=True
        ),
    ]


def _make_barbers() -> list[Barber]:
    """Create sample active barbers for resolution tests."""
    return [
        Barber(id=uuid.uuid4(), name="Carlos Silva", active=True),
        Barber(id=uuid.uuid4(), name="Marcos Souza", active=True),
        Barber(id=uuid.uuid4(), name="Rafael Lima", active=True),
    ]


def test_normalize_text() -> None:
    """Verify normalize_text strips diacritics, punctuation and collapses whitespace."""
    assert normalize_text("Degradê!") == "degrade"
    assert normalize_text("  Corte   de   Cabelo  ") == "corte de cabelo"
    assert normalize_text("Barba & Cabelo") == "barba cabelo"
    assert normalize_text("Pézinho") == "pezinho"


def test_resolve_service_exact_and_accent_insensitive() -> None:
    """Verify exact service matching with different casing and diacritics."""
    resolver = EntityResolver()
    services = _make_services()

    res = resolver.resolve_service("corte de cabelo", services)
    assert res.matched is True
    assert res.entity is not None
    assert res.entity.name == "Corte de Cabelo"

    res_accent = resolver.resolve_service("CORTE DE CABELO", services)
    assert res_accent.matched is True
    assert res_accent.entity is not None
    assert res_accent.entity.name == "Corte de Cabelo"


def test_resolve_service_aliases() -> None:
    """Verify alias mapping for common barbershop vernacular."""
    resolver = EntityResolver()
    services = _make_services()

    # "degrade" and "social" map to Corte de Cabelo
    res_degrade = resolver.resolve_service("degradê", services)
    assert res_degrade.matched is True
    assert res_degrade.entity is not None
    assert res_degrade.entity.name == "Corte de Cabelo"

    # "pezinho" and "barboterapia" map to Barba
    res_barba = resolver.resolve_service("barboterapia", services)
    assert res_barba.matched is True
    assert res_barba.entity is not None
    assert res_barba.entity.name == "Barba"

    # "combo" maps to Corte e Barba
    res_combo = resolver.resolve_service("combo completo", services)
    assert res_combo.matched is True
    assert res_combo.entity is not None
    assert res_combo.entity.name == "Corte e Barba"


def test_resolve_service_fuzzy_match() -> None:
    """Verify fuzzy matching handles minor typos with similarity >= 0.80."""
    resolver = EntityResolver()
    services = _make_services()

    # Typo in service name: "cortee de cabeloo"
    res = resolver.resolve_service("cortee de cabeloo", services)
    assert res.matched is True
    assert res.entity is not None
    assert res.entity.name == "Corte de Cabelo"


def test_resolve_service_zero_matches() -> None:
    """Verify zero matches returns matched=False and lists available services."""
    resolver = EntityResolver()
    services = _make_services()

    res = resolver.resolve_service("massagem relaxante", services)
    assert res.matched is False
    assert res.entity is None
    assert len(res.candidates) == len(services)
    assert res.ambiguous is False


def test_resolve_barber_no_preference() -> None:
    """Verify explicit or omitted barber preference returns matched=True with entity=None."""
    resolver = EntityResolver()
    barbers = _make_barbers()

    assert resolver.resolve_barber(None, barbers).entity is None
    assert resolver.resolve_barber("sem preferência", barbers).entity is None
    assert resolver.resolve_barber("tanto faz", barbers).entity is None
    assert resolver.resolve_barber("qualquer um", barbers).entity is None
    assert resolver.resolve_barber("qualquer barbeiro", barbers).entity is None


def test_resolve_barber_first_name_and_exact() -> None:
    """Verify barber resolution by first name or full name."""
    resolver = EntityResolver()
    barbers = _make_barbers()

    # Match by first name
    res_carlos = resolver.resolve_barber("carlos", barbers)
    assert res_carlos.matched is True
    assert res_carlos.entity is not None
    assert res_carlos.entity.name == "Carlos Silva"

    # Match by full name
    res_marcos = resolver.resolve_barber("Marcos Souza", barbers)
    assert res_marcos.matched is True
    assert res_marcos.entity is not None
    assert res_marcos.entity.name == "Marcos Souza"


def test_resolve_barber_fuzzy_match() -> None:
    """Verify fuzzy matching on barber names."""
    resolver = EntityResolver()
    barbers = _make_barbers()

    # Slight typo "Karlos"
    res = resolver.resolve_barber("Karlos", barbers)
    assert res.matched is True
    assert res.entity is not None
    assert res.entity.name == "Carlos Silva"


def test_resolve_barber_ambiguity() -> None:
    """Verify multiple barbers sharing a first name triggers ambiguous=True."""
    resolver = EntityResolver()
    barbers = [
        Barber(id=uuid.uuid4(), name="Carlos Silva", active=True),
        Barber(id=uuid.uuid4(), name="Carlos Oliveira", active=True),
    ]

    res = resolver.resolve_barber("carlos", barbers)
    assert res.matched is False
    assert res.ambiguous is True
    assert len(res.candidates) == 2


def test_resolve_barber_zero_matches() -> None:
    """Verify unknown barber returns matched=False and candidate list."""
    resolver = EntityResolver()
    barbers = _make_barbers()

    res = resolver.resolve_barber("Zezinho", barbers)
    assert res.matched is False
    assert res.entity is None
    assert len(res.candidates) == len(barbers)
