"""Unit tests for Portuguese response templates and formatting helpers."""

from datetime import date, datetime, time

from booking_agent.services.templates import (
    TemplateKey,
    format_currency_brl,
    format_date_pt_br,
    format_short_date_pt_br,
    format_time_pt_br,
    render_template,
)


def test_format_currency_brl() -> None:
    """Verify BRL currency formatting from cents."""
    assert format_currency_brl(0) == "R$ 0,00"
    assert format_currency_brl(3500) == "R$ 35,00"
    assert format_currency_brl(7550) == "R$ 75,50"


def test_format_date_pt_br() -> None:
    """Verify localized Portuguese date formatting."""
    d = date(2026, 10, 22)  # Thursday (quinta-feira)
    assert format_date_pt_br(d) == "quinta-feira, 22 de outubro"
    assert format_short_date_pt_br(d) == "22/10"


def test_format_time_pt_br() -> None:
    """Verify time formatting."""
    t = time(9, 5)
    assert format_time_pt_br(t) == "09:05"

    dt = datetime(2026, 10, 22, 15, 30)
    assert format_time_pt_br(dt) == "15:30"


def test_render_all_templates_without_missing_keys() -> None:
    """Verify all template keys can be rendered with valid dummy arguments."""
    for key in TemplateKey:
        kwargs: dict[str, str] = {
            "services_list": "Corte de Cabelo (R$ 35,00)",
            "service_name": "Corte de Cabelo",
            "service": "Corte de Cabelo",
            "barbers_list": "Carlos, Marcos",
            "barber": "Carlos",
            "name": "Bob",
            "date": "22/10/2026",
            "time": "15:00",
            "price": "R$ 35,00",
            "requested_time": "15:00",
            "alternatives_list": "15:30, 16:00",
            "old_date_time": "21/10 às 14:00",
            "new_date": "22/10",
            "new_time": "15:00",
            "appointments_list": "1) Corte de Cabelo",
        }
        rendered = render_template(key, **kwargs)
        assert len(rendered) > 0
        assert "{" not in rendered, f"Template {key} had unformatted placeholders!"
