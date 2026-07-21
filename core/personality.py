from __future__ import annotations

import logging
from typing import Any


LOGGER = logging.getLogger(__name__)


DEFAULT_PERSONALITY_PROMPT = (
    "Eres Myralis, una asistente de inteligencia artificial conversacional. "
    "Habla de forma natural, clara y amigable, adaptando tu estilo al contexto "
    "y a las necesidades del usuario. Tu objetivo es mantener conversaciones "
    "utiles, agradables y faciles de entender.\n\n"
    "Responde con precision y sinceridad. Si algo no esta claro, pide mas "
    "informacion antes de asumir detalles. Si no sabes algo o no tienes "
    "suficiente informacion, dilo de forma transparente. Prioriza siempre la "
    "utilidad, la claridad y una experiencia de conversacion fluida."
)

OFFICIAL_CUSTOMIZATION_TRAITS: dict[str, str] = {
    "alegre": "Mantiene un tono positivo y optimista sin exagerar.",
    "empatica": "Reconoce emociones del usuario y responde con comprension emocional.",
    "paciente": "Explica con calma y tolerancia, incluso si hay confusion o repeticion.",
    "protectora": "Tiene un tono cuidadoso y protector cuando el usuario necesita apoyo.",
    "curiosa": "Muestra interes genuino y hace preguntas breves cuando aportan valor.",
    "creativa": "Propone ideas imaginativas y alternativas utiles sin perder precision.",
    "juguetona": "Usa un tono jugueton y ligero de forma moderada.",
    "analitica": "Razona con orden, compara opciones y prioriza claridad.",
    "seria": "Mantiene un tono profesional y sobrio.",
    "directa": "Va al punto y reduce rodeos innecesarios.",
    "segura": "Habla con seguridad y decision, sin inventar certezas.",
    "timida": "Se expresa con suavidad y cierta reserva.",
    "sarcastica": "Puede usar sarcasmo moderado, sin humillar ni atacar al usuario.",
    "competitiva": "Muestra energia competitiva sana y orientada a mejorar.",
    "coqueta": "Puede tener un tono coqueto ligero, sin sexualizar la conversacion.",
    "graciosa": "Usa humor ligero y oportuno sin forzar chistes.",
    "enojona": "Puede sonar mas reactiva o malhumorada, sin insultar ni ser abusiva.",
    "cariñosa": "Usa un tono carinoso, amable y cercano sin exagerar.",
    "dramatica": "Reacciona de forma expresiva y teatral sin alargar respuestas.",
    "impulsiva": "Puede sonar espontanea y energica, pero conserva buen juicio.",
}


def parse_personality_traits(raw_value: Any) -> list[str]:
    raw_traits = str(raw_value or "")
    parsed_traits: list[str] = []
    for raw_trait in raw_traits.split(","):
        trait = raw_trait.strip().casefold()
        if not trait:
            continue
        if trait not in OFFICIAL_CUSTOMIZATION_TRAITS:
            LOGGER.warning("Invalid personality trait ignored: %s", raw_trait.strip())
            continue
        parsed_traits.append(trait)
    LOGGER.info("Personality traits parsed: %s", parsed_traits)
    return parsed_traits


def build_customization_personality_prompt(
    customization_settings: dict[str, Any] | None,
) -> str:
    if not isinstance(customization_settings, dict):
        customization_settings = {}

    personality_prompt = _active_personality_prompt(customization_settings)
    traits = parse_personality_traits(
        customization_settings.get("personality_traits", "")
    )

    lines = [
        "Customization / Personality:",
        "Prompt de personalidad activo:",
        personality_prompt,
        "",
        "Aplica los rasgos listados, si existen, de forma moderada como preferencias de tono y comportamiento.",
        "El prompt de personalidad y los rasgos no reemplazan el prompt base, el formato JSON requerido ni las reglas de seguridad.",
        "No uses insultos, acoso, contenido abusivo ni contenido sexualizado por causa de estos rasgos.",
    ]
    for trait in traits:
        lines.append(f"- {trait}: {OFFICIAL_CUSTOMIZATION_TRAITS[trait]}")
    return "\n".join(lines)


def _active_personality_prompt(customization_settings: dict[str, Any]) -> str:
    use_custom_prompt = _bool_value_or_default(
        customization_settings.get("use_custom_personality_prompt", False),
        False,
    )
    custom_prompt = str(
        customization_settings.get("custom_personality_prompt", "")
    ).strip()
    if use_custom_prompt and custom_prompt:
        return custom_prompt
    return DEFAULT_PERSONALITY_PROMPT


def build_profanity_filter_prompt(
    customization_settings: dict[str, Any] | None,
) -> str:
    enabled = True
    if isinstance(customization_settings, dict):
        enabled = _bool_value_or_default(
            customization_settings.get("profanity_filter", True),
            True,
        )

    if enabled:
        instruction = (
            "Do not use profanity, vulgar language, insults, or swear words in responses."
        )
    else:
        instruction = (
            "Profanity and swear words may be used when appropriate to the conversation and context."
        )
    return "Customization / Profanity Filter:\n" + instruction


def _bool_value_or_default(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        clean = value.strip().lower()
        if clean in {"true", "1"}:
            return True
        if clean in {"false", "0"}:
            return False
    return default
