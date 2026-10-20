"""Deterministic Brazilian Portuguese response templates and formatting helpers."""

from datetime import date, datetime, time
from enum import StrEnum

_MONTH_NAMES_PT: list[str] = [
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
]

_WEEKDAY_NAMES_PT: list[str] = [
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
]


class TemplateKey(StrEnum):
    """Enumeration of deterministic customer response templates."""

    GREETING_HELP = "greeting_help"
    ASK_SERVICE = "ask_service"
    ASK_DATE_TIME = "ask_date_time"
    ASK_BARBER = "ask_barber"
    ASK_CUSTOMER_NAME = "ask_customer_name"
    OFFER_ALTERNATIVES = "offer_alternatives"
    CONFIRM_BOOKING = "confirm_booking"
    BOOKING_SUCCESS = "booking_success"
    CONFIRM_RESCHEDULE = "confirm_reschedule"
    RESCHEDULE_SUCCESS = "reschedule_success"
    CONFIRM_CANCEL = "confirm_cancel"
    CANCEL_SUCCESS = "cancel_success"
    FLOW_CANCELLED = "flow_cancelled"
    CHOOSE_APPOINTMENT = "choose_appointment"
    NO_APPOINTMENTS_FOUND = "no_appointments_found"
    SERVICE_NOT_FOUND = "service_not_found"
    BARBER_NOT_FOUND = "barber_not_found"
    CLARIFICATION_PROMPT = "clarification_prompt"
    SESSION_EXPIRED = "session_expired"
    REQUEST_PROCESSING = "request_processing"


_TEMPLATES: dict[TemplateKey, str] = {
    TemplateKey.GREETING_HELP: (
        "Olá! Sou o assistente da Northside Barbers. Posso ajudar você a agendar, "
        "remarcar ou cancelar seu horário. O que deseja fazer?"
    ),
    TemplateKey.ASK_SERVICE: ("Qual serviço você gostaria de realizar? Temos: {services_list}."),
    TemplateKey.ASK_DATE_TIME: (
        "Para qual dia e horário você gostaria de agendar o seu {service_name}?"
    ),
    TemplateKey.ASK_BARBER: (
        "Você tem preferência por algum barbeiro? ({barbers_list} ou sem preferência)"
    ),
    TemplateKey.ASK_CUSTOMER_NAME: ("Por favor, qual é o seu nome para o agendamento?"),
    TemplateKey.OFFER_ALTERNATIVES: (
        "O horário das {requested_time} já está ocupado. Temos estes horários disponíveis "
        "no mesmo dia: {alternatives_list}. Algum deles fica bom para você?"
    ),
    TemplateKey.CONFIRM_BOOKING: (
        "Podemos confirmar? Agendamento de {service} em {date} às {time} com {barber} "
        "para {name}. Valor: {price}. (Responda 'sim' para confirmar ou informe se "
        "deseja mudar algo)"
    ),
    TemplateKey.BOOKING_SUCCESS: (
        "Perfeito, {name}! Seu horário para {service} está confirmado para {date} às {time} "
        "com {barber}. Te esperamos lá!"
    ),
    TemplateKey.CONFIRM_RESCHEDULE: (
        "Podemos confirmar a remarcação de {service} de {old_date_time} para {new_date} "
        "às {new_time}?"
    ),
    TemplateKey.RESCHEDULE_SUCCESS: (
        "Seu agendamento foi remarcado com sucesso para {new_date} às {new_time} com {barber}!"
    ),
    TemplateKey.CONFIRM_CANCEL: (
        "Deseja realmente cancelar seu agendamento de {service} no dia {date} às {time} "
        "com {barber}?"
    ),
    TemplateKey.CANCEL_SUCCESS: (
        "Seu agendamento de {service} no dia {date} às {time} foi cancelado com sucesso."
    ),
    TemplateKey.FLOW_CANCELLED: (
        "Tudo bem, a solicitação foi cancelada. Se precisar de mais alguma coisa, estou por aqui!"
    ),
    TemplateKey.CHOOSE_APPOINTMENT: (
        "Você possui os seguintes agendamentos futuros:\n{appointments_list}\n"
        "Qual deles você gostaria de selecionar? (Responda com o número)"
    ),
    TemplateKey.NO_APPOINTMENTS_FOUND: (
        "Não encontrei nenhum agendamento futuro confirmado para o seu número. "
        "Gostaria de agendar um novo horário?"
    ),
    TemplateKey.SERVICE_NOT_FOUND: (
        "Não encontrei esse serviço. Nossos serviços disponíveis são: {services_list}. "
        "Qual você gostaria?"
    ),
    TemplateKey.BARBER_NOT_FOUND: (
        "Não encontrei esse profissional. Nossos barbeiros disponíveis são: {barbers_list}. "
        "Deseja escolher um deles ou podemos marcar com qualquer barbeiro disponível?"
    ),
    TemplateKey.CLARIFICATION_PROMPT: (
        "Desculpe, não consegui entender bem a sua mensagem. Você poderia explicar de outra "
        "forma o que gostaria de fazer?"
    ),
    TemplateKey.SESSION_EXPIRED: (
        "Olá! Sua sessão anterior expirou por inatividade. Como posso ajudar você hoje? "
        "(Agendar, remarcar ou cancelar)"
    ),
    TemplateKey.REQUEST_PROCESSING: (
        "Seu pedido está sendo processado, por favor aguarde um instante."
    ),
}


def format_currency_brl(price_cents: int) -> str:
    """Format integer price in cents to Brazilian Real (e.g. 3500 -> 'R$ 35,00')."""
    reais = price_cents / 100.0
    return f"R$ {reais:.2f}".replace(".", ",")


def format_date_pt_br(target: date | datetime) -> str:
    """Format date to localized Portuguese format (e.g. 'quinta-feira, 22 de outubro')."""
    d = target.date() if isinstance(target, datetime) else target
    weekday = _WEEKDAY_NAMES_PT[d.weekday()]
    month = _MONTH_NAMES_PT[d.month - 1]
    return f"{weekday}, {d.day} de {month}"


def format_short_date_pt_br(target: date | datetime) -> str:
    """Format date to short DD/MM format (e.g. '22/10')."""
    d = target.date() if isinstance(target, datetime) else target
    return f"{d.day:02d}/{d.month:02d}"


def format_time_pt_br(target: time | datetime) -> str:
    """Format time to Brazilian notation (e.g. '15:00')."""
    t = target.time() if isinstance(target, datetime) else target
    return f"{t.hour:02d}:{t.minute:02d}"


def render_template(key: TemplateKey, **kwargs: object) -> str:
    """Render a deterministic Portuguese response template with supplied arguments.

    Args:
        key: The template identifier from TemplateKey.
        **kwargs: Substitution parameters expected by the template string.

    Returns:
        Rendered message string ready for user presentation.
    """
    tmpl = _TEMPLATES[key]
    return tmpl.format(**kwargs) if kwargs else tmpl
