from __future__ import annotations

import random
import re


VALID_MOODS: tuple[str, ...] = (
    "Neutral",
    "Happy",
    "Sad",
    "Disgust",
    "Anger",
    "Surprise",
    "Fear",
    "Confident",
    "Excited",
    "Bored",
    "Playful",
    "Confused",
)

DEFAULT_MOOD = "Neutral"

ALLOWED_RUNTIME_MOODS: set[str] = {
    "Neutral",
    "Happy",
    "Sad",
    "Anger",
    "Surprise",
    "Fear",
    "Confident",
    "Excited",
    "Bored",
    "Playful",
    "Confused",
    "Disgust",
}

EMOTION_STRENGTH_BY_MOOD: dict[str, float] = {
    "Neutral": 0.30,
    "Happy": 0.65,
    "Sad": 0.60,
    "Anger": 0.70,
    "Surprise": 0.70,
    "Fear": 0.65,
    "Confident": 0.60,
    "Excited": 0.80,
    "Bored": 0.35,
    "Playful": 0.65,
    "Confused": 0.55,
    "Disgust": 0.60,
}

_RUNTIME_MOOD_BY_LOWER: dict[str, str] = {
    mood.lower(): mood for mood in ALLOWED_RUNTIME_MOODS
}
_RUNTIME_MOOD_ALIASES: dict[str, str] = {
    "angry": "Anger",
    "surprised": "Surprise",
    "fearful": "Fear",
    "concerned": "Confused",
    "worried": "Sad",
    "calm": "Neutral",
    "curious": "Confident",
}

ELEVENLABS_MOOD_PROFILES: dict[str, dict[str, float | bool]] = {
    "Neutral": {
        "stability": 0.55,
        "similarity_boost": 0.75,
        "style": 0.00,
        "use_speaker_boost": True,
        "speed": 1.00,
    },
    "Happy": {
        "stability": 0.38,
        "similarity_boost": 0.73,
        "style": 0.38,
        "use_speaker_boost": True,
        "speed": 1.05,
    },
    "Sad": {
        "stability": 0.48,
        "similarity_boost": 0.78,
        "style": 0.40,
        "use_speaker_boost": True,
        "speed": 0.85,
    },
    "Disgust": {
        "stability": 0.45,
        "similarity_boost": 0.74,
        "style": 0.38,
        "use_speaker_boost": True,
        "speed": 0.95,
    },
    "Anger": {
        "stability": 0.28,
        "similarity_boost": 0.76,
        "style": 0.60,
        "use_speaker_boost": True,
        "speed": 1.08,
    },
    "Surprise": {
        "stability": 0.28,
        "similarity_boost": 0.74,
        "style": 0.52,
        "use_speaker_boost": True,
        "speed": 1.12,
    },
    "Fear": {
        "stability": 0.35,
        "similarity_boost": 0.76,
        "style": 0.45,
        "use_speaker_boost": True,
        "speed": 1.06,
    },
    "Confident": {
        "stability": 0.58,
        "similarity_boost": 0.80,
        "style": 0.18,
        "use_speaker_boost": True,
        "speed": 1.01,
    },
    "Excited": {
        "stability": 0.26,
        "similarity_boost": 0.73,
        "style": 0.68,
        "use_speaker_boost": True,
        "speed": 1.14,
    },
    "Bored": {
        "stability": 0.80,
        "similarity_boost": 0.75,
        "style": 0.05,
        "use_speaker_boost": False,
        "speed": 0.90,
    },
    "Playful": {
        "stability": 0.34,
        "similarity_boost": 0.74,
        "style": 0.54,
        "use_speaker_boost": True,
        "speed": 1.08,
    },
    "Confused": {
        "stability": 0.48,
        "similarity_boost": 0.75,
        "style": 0.30,
        "use_speaker_boost": True,
        "speed": 0.98,
    },
}


def normalize_mood(mood: str | None) -> str:
    if not isinstance(mood, str):
        return DEFAULT_MOOD

    clean_mood = mood.strip()
    for valid_mood in VALID_MOODS:
        if clean_mood.lower() == valid_mood.lower():
            return valid_mood
    return DEFAULT_MOOD


def normalize_runtime_mood(mood: str | None) -> str:
    if not isinstance(mood, str):
        return DEFAULT_MOOD

    clean_mood = mood.strip()
    lowered = clean_mood.lower()
    return _RUNTIME_MOOD_BY_LOWER.get(
        lowered,
        _RUNTIME_MOOD_ALIASES.get(lowered, DEFAULT_MOOD),
    )


def get_emotion_strength_for_mood(mood: str | None) -> float:
    clean_mood = normalize_runtime_mood(mood)
    return float(EMOTION_STRENGTH_BY_MOOD[clean_mood])


def get_random_debug_mood() -> str:
    moods = sorted(ALLOWED_RUNTIME_MOODS)
    return random.choice(moods)


def detect_response_mood(
    response_text: str,
    previous_mood: str = DEFAULT_MOOD,
    user_text: str | None = None,
) -> str:
    previous = normalize_mood(previous_mood)
    text = response_text.strip()
    if not text:
        return previous if previous != DEFAULT_MOOD else DEFAULT_MOOD

    lowered = text.lower()
    user_lowered = (user_text or "").strip().lower()

    if previous == "Anger" and _user_deescalates(user_lowered):
        if _contains_any(lowered, HAPPY_TERMS):
            return "Happy"
        if _looks_technical_or_instructive(lowered):
            return "Confident"
        return DEFAULT_MOOD

    if previous == "Anger" and _user_confronts(user_lowered):
        if not _contains_any(lowered, HAPPY_TERMS + APOLOGY_TERMS):
            return "Anger"

    if previous == "Sad" and _user_positive_or_comforting(user_lowered):
        return "Happy" if _contains_any(lowered, HAPPY_TERMS) else DEFAULT_MOOD

    if previous == "Fear" and _risk_seems_resolved(lowered):
        return "Confident" if _looks_technical_or_instructive(lowered) else DEFAULT_MOOD

    if previous == "Confused" and _looks_technical_or_instructive(lowered):
        if not _contains_any(lowered, CONFUSED_TERMS):
            return "Confident"

    scores: dict[str, int] = {mood: 0 for mood in VALID_MOODS}

    _score_terms(scores, lowered, "Disgust", DISGUST_TERMS, weight=4)
    _score_terms(scores, lowered, "Anger", ANGER_TERMS, weight=4)
    _score_terms(scores, lowered, "Fear", FEAR_TERMS, weight=4)
    _score_terms(scores, lowered, "Sad", SAD_TERMS, weight=3)
    _score_terms(scores, lowered, "Surprise", SURPRISE_TERMS, weight=3)
    _score_terms(scores, lowered, "Confused", CONFUSED_TERMS, weight=3)
    _score_terms(scores, lowered, "Playful", PLAYFUL_TERMS, weight=3)
    _score_terms(scores, lowered, "Excited", EXCITED_TERMS, weight=3)
    _score_terms(scores, lowered, "Happy", HAPPY_TERMS, weight=2)
    _score_terms(scores, lowered, "Confident", CONFIDENT_TERMS, weight=2)
    _score_terms(scores, lowered, "Bored", BORED_TERMS, weight=5)

    exclamation_count = text.count("!")
    if exclamation_count >= 2:
        scores["Excited"] += 3
    elif exclamation_count == 1 and _contains_any(lowered, HAPPY_TERMS + EXCITED_TERMS):
        scores["Excited"] += 1

    if _short_positive_confirmation(lowered):
        scores["Happy"] += 4

    if _looks_technical_or_instructive(lowered):
        scores["Confident"] += 3

    if previous == "Excited" and _conversation_still_positive(lowered, user_lowered):
        scores["Excited"] += 2
        scores["Happy"] += 1

    if previous == "Anger" and not _user_deescalates(user_lowered):
        scores["Anger"] += 1

    for strong_mood in ("Disgust", "Anger", "Fear"):
        if scores[strong_mood] >= 4:
            return strong_mood

    detected = max(scores, key=scores.get)
    if scores[detected] > 0:
        return detected

    if previous != DEFAULT_MOOD:
        return previous
    return DEFAULT_MOOD


def prepare_text_for_elevenlabs(text: str, mood: str) -> str:
    clean_text = text.strip()
    if not clean_text:
        return text

    clean_mood = normalize_mood(mood)
    if clean_mood == "Neutral" or _contains_code_or_sensitive_text(clean_text):
        return clean_text

    prepared = _normalize_repeated_punctuation(clean_text)

    if clean_mood in {"Sad", "Bored"}:
        prepared = re.sub(r"!+", ".", prepared)
        if clean_mood == "Sad":
            prepared = _soften_to_sad_tone(prepared)
    elif clean_mood == "Fear":
        prepared = re.sub(r"!{2,}", "!", prepared)
    elif clean_mood == "Anger":
        prepared = re.sub(r"!{2,}", "!", prepared)
    elif clean_mood in {"Happy", "Playful", "Surprise", "Excited"}:
        prepared = re.sub(r"!{3,}", "!!", prepared)
        if clean_mood in {"Happy", "Excited"} and _can_lightly_emphasize(prepared):
            prepared = _replace_final_period(prepared, "!")

    return prepared


def _score_terms(
    scores: dict[str, int],
    text: str,
    mood: str,
    terms: tuple[str, ...],
    *,
    weight: int,
) -> None:
    scores[mood] += sum(weight for term in terms if term in text)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _short_positive_confirmation(text: str) -> bool:
    if len(text) > 120:
        return False
    return _contains_any(
        text,
        (
            "listo",
            "claro",
            "perfecto",
            "hecho",
            "va",
            "ok",
            "muy bien",
            "excelente",
            "genial",
        ),
    )


def _looks_technical_or_instructive(text: str) -> bool:
    if re.search(r"(^|\n)\s*(\d+\.|- )\s+", text):
        return True
    return _contains_any(
        text,
        (
            "codigo",
            "code",
            "archivo",
            "funcion",
            "function",
            "clase",
            "class",
            "metodo",
            "method",
            "variable",
            "runtime",
            "json",
            "wav",
            "elevenlabs",
            "openai",
            "unreal",
            "implement",
            "modifique",
            "actualice",
            "verifique",
            "solucion",
            "error",
            "payload",
            "request",
            "settings",
            "configuracion",
            "pasos",
            "prueba",
            "test",
            "log",
        ),
    )


def _contains_code_or_sensitive_text(text: str) -> bool:
    if "```" in text or "`" in text:
        return True
    if re.search(r"(^|\n)\s*(def|class|import|from|return|if|for|while)\s+", text):
        return True
    if re.search(r"[A-Za-z]:\\|[/\\][\w.-]+[/\\][\w.-]+", text):
        return True
    if re.search(r"https?://", text):
        return True
    if re.search(r"\{[^{}]*:[^{}]*\}", text):
        return True
    return False


def _normalize_repeated_punctuation(text: str) -> str:
    text = re.sub(r"\?{3,}", "??", text)
    text = re.sub(r"\.{4,}", "...", text)
    return text


def _can_lightly_emphasize(text: str) -> bool:
    if len(text) > 180 or text.endswith(("!", "?", "...")):
        return False
    lowered = text.lower()
    return _contains_any(lowered, HAPPY_TERMS + EXCITED_TERMS)


def _replace_final_period(text: str, replacement: str) -> str:
    if text.endswith("."):
        return text[:-1] + replacement
    return text


def _soften_to_sad_tone(text: str) -> str:
    if len(text) > 180:
        return text
    if text.endswith(("...", "?", "!")):
        return text
    if text.endswith("."):
        return text[:-1] + "..."
    return text + "..."


def _user_confronts(user_text: str) -> bool:
    return _contains_any(user_text, CONFRONTING_USER_TERMS)


def _user_deescalates(user_text: str) -> bool:
    return _contains_any(user_text, APOLOGY_TERMS + CALMING_USER_TERMS)


def _user_positive_or_comforting(user_text: str) -> bool:
    return _contains_any(user_text, HAPPY_TERMS + CALMING_USER_TERMS)


def _risk_seems_resolved(text: str) -> bool:
    return _contains_any(
        text,
        (
            "resuelto",
            "resolved",
            "ya quedo",
            "ya esta",
            "sin riesgo",
            "seguro",
            "safe",
            "corregido",
            "controlado",
        ),
    )


def _conversation_still_positive(text: str, user_text: str) -> bool:
    return _contains_any(text + " " + user_text, HAPPY_TERMS + EXCITED_TERMS)


HAPPY_TERMS: tuple[str, ...] = (
    "alegr",
    "feliz",
    "me gusta",
    "bien hecho",
    "muy bien",
    "excelente",
    "genial",
    "perfecto",
    "gracias",
    "con gusto",
    "warm",
    "happy",
    "great",
    "nice",
    "excellent",
    "glad",
)

EXCITED_TERMS: tuple[str, ...] = (
    "emocion",
    "increible",
    "brillante",
    "celebr",
    "vamos",
    "wow",
    "fantastico",
    "impresionante",
    "excited",
    "amazing",
    "awesome",
    "fantastic",
    "celebrate",
)

CONFIDENT_TERMS: tuple[str, ...] = (
    "claro",
    "directo",
    "seguro",
    "concreto",
    "te recomiendo",
    "la solucion",
    "el enfoque",
    "correcto",
    "confirmo",
    "clear",
    "direct",
    "recommended",
    "solution",
)

PLAYFUL_TERMS: tuple[str, ...] = (
    "jaja",
    "jeje",
    "broma",
    "jugueton",
    "divertido",
    "guino",
    "haha",
    "joke",
    "playful",
    "funny",
)

CONFUSED_TERMS: tuple[str, ...] = (
    "no estoy segura",
    "no tengo claro",
    "confuso",
    "confund",
    "duda",
    "incierto",
    "puede que",
    "creo que",
    "no se",
    "unclear",
    "confused",
    "unsure",
    "uncertain",
)

SURPRISE_TERMS: tuple[str, ...] = (
    "sorpresa",
    "sorprend",
    "inesperado",
    "vaya",
    "oh,",
    "no esperaba",
    "surprise",
    "unexpected",
)

SAD_TERMS: tuple[str, ...] = (
    "triste",
    "lo siento",
    "lamento",
    "perdida",
    "dolor",
    "frustracion",
    "bajon",
    "sad",
    "sorry",
    "loss",
    "grief",
    "frustrated",
)

FEAR_TERMS: tuple[str, ...] = (
    "miedo",
    "temor",
    "peligro",
    "riesgo fuerte",
    "preocupacion fuerte",
    "grave",
    "amenaza",
    "danger",
    "fear",
    "risk",
    "unsafe",
    "threat",
)

ANGER_TERMS: tuple[str, ...] = (
    "enojo",
    "enoj",
    "molest",
    "irrit",
    "confront",
    "no acepto",
    "no voy a tolerar",
    "angry",
    "anger",
    "mad",
    "upset",
)

DISGUST_TERMS: tuple[str, ...] = (
    "asco",
    "repulsion",
    "repulsiv",
    "desagrad",
    "rechazo fuerte",
    "disgust",
    "gross",
    "repulsive",
)

BORED_TERMS: tuple[str, ...] = (
    "aburr",
    "desinteres",
    "monotono",
    "cansado de esto",
    "bored",
    "boring",
    "monotone",
    "tired of this",
)

APOLOGY_TERMS: tuple[str, ...] = (
    "perdon",
    "perdona",
    "disculpa",
    "lo siento",
    "me disculpo",
    "sorry",
    "apologize",
)

CALMING_USER_TERMS: tuple[str, ...] = (
    "tranquila",
    "calma",
    "bajemos",
    "no fue mi intencion",
    "esta bien",
    "te entiendo",
    "calm",
    "peace",
    "i understand",
)

CONFRONTING_USER_TERMS: tuple[str, ...] = (
    "callate",
    "tonta",
    "idiota",
    "inutil",
    "no sirves",
    "estas mal",
    "basura",
    "shut up",
    "stupid",
    "useless",
    "trash",
)
