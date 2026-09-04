# =============================================================================
# rtn_compat.py — integrity shim for the RTN / PTT release-name parser
# =============================================================================
# Why this module exists (a real, shipped outage):
#
# PTT (the parser behind RTN) registers `PTT.adult.is_adult_content` as a
# DEFAULT handler, so it runs on every single parse. That handler calls
# `load_adult_keywords()`, which reads PTT/keywords/*.txt off disk relative to
# `PTT.__file__`. A PyInstaller build that bundles the PTT *modules* (they live
# in the PYZ) but not those *data files* therefore raises FileNotFoundError on
# EVERY parse.
#
# torrent_select's gate 1 wrapped `parse()` in a bare `except Exception: parsed
# = None`, which coded that failure as the candidate's own fault:
# "unparseable". The 1.6.0 EXE shipped without the keyword files, so from the
# moment it launched, every candidate from every source was rejected as
# unparseable and nothing could be grabbed — automatically or manually — with
# no error anywhere in the log.
#
# Two defences, both here:
#   1. ensure_parser() proves the parser works at startup and, when the data
#      files are missing, neutralises the adult-keyword handler so parsing
#      still works (a degraded 'adult' flag is a rounding error next to a
#      totally dead selection engine).
#   2. parse_release() separates "the parser blew up" (parse_error, our bug)
#      from "this title has no recoverable name" (unparseable, the release's
#      fault) and LOGS the former instead of swallowing it.
#
# The spec files bundle the data properly now (collect_data_files("PTT")); this
# shim is the belt to that braces, because a silent total parse failure is the
# worst failure mode this app has.
# =============================================================================

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# A title whose parse exercises the default handler chain (adult keywords
# included) without depending on any particular release group or pattern.
_PROBE_TITLE = "Probe Title 2020 1080p BluRay x264-GROUP"

STATUS_OK = "ok"                    # parser healthy, nothing patched
STATUS_DEGRADED = "degraded"        # patched around missing keyword data
STATUS_BROKEN = "broken"            # still cannot parse; selection is blind

_LOCK = threading.Lock()
_STATUS: Optional[str] = None
_DETAIL = ""
# One log line per distinct parse failure text, so a systemic breakage is loud
# once instead of 20,000 times.
_SEEN_ERRORS: set[str] = set()


def _probe() -> str:
    """'' when a representative title parses, else the failure description."""
    try:
        from RTN import parse
        parsed = parse(_PROBE_TITLE)
    except Exception as exc:  # pragma: no cover - exercised via the shim tests
        return f"{type(exc).__name__}: {exc}"
    title = (getattr(parsed, "parsed_title", "") or "").strip()
    return "" if title else "parse returned no title"


def _disable_adult_keywords() -> bool:
    """Replace PTT's disk-backed adult-keyword loader with an empty set.

    Returns True when the patch was applied. The handler reads
    `load_adult_keywords` out of PTT.adult's module globals at call time, so
    rebinding the name is enough — no import-order games needed.
    """
    try:
        import PTT.adult as ptt_adult
    except Exception:
        return False

    def _no_keywords(filename: str = "combined-keywords.txt"):
        return frozenset()

    try:
        cached = getattr(ptt_adult.load_adult_keywords, "cache_clear", None)
        if cached is not None:
            cached()
    except Exception:
        pass
    ptt_adult.load_adult_keywords = _no_keywords  # type: ignore[assignment]
    return True


def ensure_parser(*, force: bool = False) -> str:
    """Prove the parser works; patch around missing bundled data if not.

    Returns STATUS_OK / STATUS_DEGRADED / STATUS_BROKEN. Cheap and idempotent
    after the first call (the result is cached), so call sites can be liberal.
    """
    global _STATUS, _DETAIL
    with _LOCK:
        if _STATUS is not None and not force:
            return _STATUS

        failure = _probe()
        if not failure:
            _STATUS, _DETAIL = STATUS_OK, ""
            return _STATUS

        patched = _disable_adult_keywords()
        second = _probe() if patched else failure
        if patched and not second:
            _STATUS = STATUS_DEGRADED
            _DETAIL = failure
            logger.warning(
                "Release-name parser could not load its bundled keyword data "
                "(%s). Disabled the adult-keyword handler so parsing works; "
                "the 'adult' flag is unreliable in this build.", failure)
            return _STATUS

        _STATUS = STATUS_BROKEN
        _DETAIL = second or failure
        logger.error(
            "Release-name parser is BROKEN in this build (%s). Every candidate "
            "will be rejected as unparseable and nothing can be grabbed until "
            "this is fixed.", _DETAIL)
        return _STATUS


def parser_status() -> dict:
    """Health-panel view of the parser's integrity."""
    status = ensure_parser()
    return {"status": status, "detail": _DETAIL,
            "healthy": status in (STATUS_OK, STATUS_DEGRADED)}


def parse_release(title: str):
    """Parse a release title. Returns (parsed_or_None, reason_code).

    reason_code is '' on success, 'unparseable' when the parser ran but found
    no title, and 'parse_error' when the parser itself raised — the distinction
    that would have made the 1.6.0 outage self-evident.
    """
    text = (title or "").strip()
    if not text:
        return None, "unparseable"
    ensure_parser()
    try:
        from RTN import parse
        parsed = parse(text)
    except Exception as exc:
        key = f"{type(exc).__name__}: {exc}"
        with _LOCK:
            first = key not in _SEEN_ERRORS
            _SEEN_ERRORS.add(key)
        if first:
            logger.error(
                "Release-name parser raised on %r (%s). Candidates are being "
                "rejected as parse_error, not because the releases are bad.",
                text, key)
        return None, "parse_error"
    if not (getattr(parsed, "parsed_title", "") or "").strip():
        return None, "unparseable"
    return parsed, ""
