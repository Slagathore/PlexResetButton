# =============================================================================
# llm_service.py
# =============================================================================
# Ollama wrapper for two tasks:
#
#   1. fuzzy_correct_title()  — given user input + DB candidates, pick the best
#      match.  Falls back to rapidfuzz when Ollama is unavailable.
#
#   2. categorize_other_request()  — for the "Other" request type, Ollama reads
#      the user's free-form text and returns structured JSON: category, guessed
#      title, reasoning, and an adult-content flag.
#
#   3. translate_title_to_english()  — a native-script title ("逐玉") searched
#      verbatim finds nothing on trackers that index English release names,
#      while its English title ("Pursuit of Jade") has whole seasons sitting
#      there. Cached; returns None when Ollama is not running.
#
# Ollama must be running locally (default: http://localhost:11434).
# Pull the desired model first, e.g.:  ollama pull gemma3:1b
#
# All functions are synchronous (blocking).  Call them from a thread executor
# so the async Telegram event loop is never stalled.
# =============================================================================

import json
import logging
import re
from typing import Any

import config

logger = logging.getLogger(__name__)

_ollama_client: Any = None   # cached ollama.Client instance
_ollama_ok: bool | None = None  # None = not yet probed


# ---------------------------------------------------------------------------
# Client initialisation
# ---------------------------------------------------------------------------

def _get_client() -> Any | None:
    global _ollama_client, _ollama_ok
    if _ollama_ok is False:
        return None
    if _ollama_client is not None:
        return _ollama_client
    try:
        import ollama  # type: ignore[import]
        client = ollama.Client(host=config.OLLAMA_HOST)
        # Quick connectivity probe — list models (fast, no GPU work)
        client.list()
        _ollama_client = client
        _ollama_ok = True
        logger.info("Ollama connected at %s, model: %s", config.OLLAMA_HOST, config.OLLAMA_MODEL)
        return client
    except Exception as exc:
        _ollama_ok = False
        logger.warning("Ollama not reachable at %s: %s — LLM features disabled.", config.OLLAMA_HOST, exc)
        return None


def llm_available() -> bool:
    """True if Ollama is running and reachable."""
    return _get_client() is not None


def _chat(prompt: str) -> str | None:
    """Send a single-turn chat to Ollama and return the reply, or None on error."""
    client = _get_client()
    if client is None:
        return None
    try:
        kwargs: dict[str, Any] = {}
        if config.OLLAMA_THINK:
            # Only sent when enabled — non-thinking models reject the parameter.
            kwargs["think"] = config.OLLAMA_THINK
        response = client.chat(
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        if config.OLLAMA_SHOW_THINKING:
            thinking = response["message"].get("thinking")
            if thinking:
                logger.info("Ollama thinking:\n%s", thinking)
        return response["message"]["content"].strip()
    except Exception as exc:
        logger.error("Ollama chat failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Rapidfuzz fallback
# ---------------------------------------------------------------------------

def _rapidfuzz_pick(query: str, candidates: list[str], threshold: float = 55.0) -> str | None:
    """Return the best-matching candidate using rapidfuzz, or None if below threshold."""
    if not candidates:
        return None
    try:
        from rapidfuzz import fuzz, process  # type: ignore[import]
        result = process.extractOne(query, candidates, scorer=fuzz.WRatio, score_cutoff=threshold)
        return result[0] if result else None
    except ImportError:
        logger.warning("rapidfuzz not installed; fuzzy fallback unavailable.")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fuzzy_correct_title(user_input: str, candidates: list[str]) -> str | None:
    """
    Given a user-typed title and a list of official titles returned by an
    external DB, ask Ollama to identify which candidate the user most likely
    meant.

    Returns the matching candidate string, or None if nothing fits.
    Falls back to rapidfuzz when Ollama is unavailable.
    """
    if not candidates:
        return None

    if not llm_available():
        return _rapidfuzz_pick(user_input, candidates)

    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(candidates))
    prompt = (
        f'A user requested: "{user_input}"\n\n'
        f"These titles were returned by a media database:\n{numbered}\n\n"
        "Which one is the user most likely asking for? "
        "Reply with ONLY the exact title string from the list above, "
        "or the word NONE if none of them are a reasonable match. "
        "Do not add any other text."
    )

    result = _chat(prompt)
    if result is None:
        return _rapidfuzz_pick(user_input, candidates)

    if result.strip().upper() == "NONE":
        return None

    # Verify the answer is actually in the candidate list
    result_clean = result.strip().casefold()
    for candidate in candidates:
        if candidate.casefold() == result_clean or result_clean in candidate.casefold():
            return candidate

    # LLM hallucinated something; fall back to rapidfuzz
    return _rapidfuzz_pick(user_input, candidates)


_TRANSLATION_CACHE: dict[str, str | None] = {}


def translate_title_to_english(title: str) -> str | None:
    """English title for a native-script one, or None when unavailable.

    Deliberately conservative: the model is asked for the title only, and any
    answer that comes back long, chatty, or still non-Latin is discarded. A
    wrong translation would send the search somewhere useless, so "no answer"
    is the safer failure.
    """
    text = (title or "").strip()
    if not text:
        return None
    if text in _TRANSLATION_CACHE:
        return _TRANSLATION_CACHE[text]

    answer: str | None = None
    if llm_available():
        prompt = (
            f'What is the English title of the film or TV series "{text}"?\n'
            "Reply with ONLY the English title, nothing else. "
            "If it has no established English title, give a literal English "
            "translation of the words. If you do not recognise it at all, "
            "reply with the single word NONE."
        )
        reply = (_chat(prompt) or "").strip().strip('"').strip()
        first = reply.splitlines()[0].strip() if reply else ""
        looks_usable = (
            first
            and first.upper() != "NONE"
            and len(first) <= 80
            and re.search(r"[A-Za-z]", first) is not None
            and len(first.split()) <= 10
        )
        answer = first if looks_usable else None
        if answer:
            logger.info("Translated title %r -> %r for searching.", text, answer)

    _TRANSLATION_CACHE[text] = answer
    return answer


def categorize_other_request(user_text: str) -> dict:
    """
    For "Other" type requests, use Ollama to:
      - Identify the most likely category.
      - Extract a guessed title (if deterministic enough).
      - Write a one-line reasoning note.
      - Flag adult / problematic content.

    Returns a dict with keys:
        category   str  — one of: movie, tv, anime, xanime, adult_film,
                          software, game, music, book, other
        title      str | None — best guess at the media/content title
        reasoning  str  — brief explanation (<=120 chars)
        flagged    bool — True for adult, potentially illegal, or privacy-sensitive content
        raw        str  — model's raw response (for dev-panel display)
    """
    if not llm_available():
        return {
            "category": "unknown",
            "title": user_text,
            "reasoning": "Ollama unavailable — manual review needed.",
            "flagged": False,
            "raw": "",
        }

    prompt = (
        "You are helping classify an uncategorized media request submitted to a "
        "home Plex media server request bot.\n\n"
        f'User\'s request: "{user_text}"\n\n'
        "Respond with ONLY valid JSON (no markdown fences) containing exactly these keys:\n"
        '  "category"  : one of [movie, tv, anime, xanime, adult_film, software, game, music, book, other]\n'
        '  "title"     : best-guess title string, or null if too vague\n'
        '  "reasoning" : one sentence, max 120 chars\n'
        '  "flagged"   : true if the request involves explicit adult content, potentially '
        "illegal material, or raises privacy concerns\n\n"
        'Example: {"category":"movie","title":"Spider-Man: Into the Spider-Verse",'
        '"reasoning":"User described the animated multiverse Spider-Man film.","flagged":false}'
    )

    raw = _chat(prompt) or ""

    # Strip markdown code fences if the model wraps it anyway
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:])
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    try:
        parsed = json.loads(cleaned)
        category = str(parsed.get("category") or "other")
        if category == "xanime" and not config.XANIME_ENABLED:
            category = "anime"  # hentai support is off — nearest sane bucket
        return {
            "category": category,
            "title": parsed.get("title"),
            "reasoning": str(parsed.get("reasoning") or "")[:200],
            "flagged": bool(parsed.get("flagged", False)),
            "raw": raw,
        }
    except (json.JSONDecodeError, TypeError, AttributeError):
        logger.warning("Ollama returned non-JSON for categorization: %.200s", raw)
        return {
            "category": "other",
            "title": user_text,
            "reasoning": f"LLM response unparseable: {raw[:80]}",
            "flagged": False,
            "raw": raw,
        }
