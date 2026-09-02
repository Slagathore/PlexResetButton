# =============================================================================
# torrent_search.py
# =============================================================================
# In-app torrent source search — replaces the "open a search page in Firefox"
# workflow with structured results the Downloads tab can grab directly.
#
# Sources by media type (mirroring torlink's source registry, but implemented
# natively in Python against each site's JSON/RSS API instead of scraping):
#   movie   → YTS (yts.mx JSON API) + The Pirate Bay (apibay.org JSON API)
#   tv      → The Pirate Bay
#   other   → The Pirate Bay
#   anime   → nyaa.si RSS
#   xanime  → sukebei.nyaa.si RSS
#
# Every source degrades gracefully to [] on failure (same pattern as
# media_lookup.py) — a dead mirror should never break the tab.
# =============================================================================

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import config

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15
_USER_AGENT = "Sensarr/1.0"

_PROVIDER_LOCK = threading.Lock()
_PROVIDER_FAILURE_UNTIL: dict[str, float] = {}
_PROVIDER_LAST_ERROR: dict[str, str] = {}


def _provider_available(provider: str) -> bool:
    """Whether a provider's failure circuit currently permits a probe."""
    now = time.monotonic()
    with _PROVIDER_LOCK:
        until = _PROVIDER_FAILURE_UNTIL.get(provider, 0.0)
        if until <= now:
            _PROVIDER_FAILURE_UNTIL.pop(provider, None)
            return True
        return False


def _record_provider_failure(provider: str, exc: Exception) -> None:
    until = time.monotonic() + max(
        1, int(config.TORRENT_PROVIDER_BACKOFF_SECONDS))
    with _PROVIDER_LOCK:
        _PROVIDER_FAILURE_UNTIL[provider] = until
        _PROVIDER_LAST_ERROR[provider] = str(exc)
    logger.warning(
        "%s search provider failed; pausing it for %ss: %s",
        provider.upper(), config.TORRENT_PROVIDER_BACKOFF_SECONDS, exc)


def _record_provider_success(provider: str) -> None:
    with _PROVIDER_LOCK:
        _PROVIDER_FAILURE_UNTIL.pop(provider, None)
        _PROVIDER_LAST_ERROR.pop(provider, None)


def provider_circuit_status(providers: list[str] | tuple[str, ...]) -> dict[str, dict]:
    """Read-only circuit detail included in selection provenance."""
    now = time.monotonic()
    with _PROVIDER_LOCK:
        return {
            provider: {
                "available": _PROVIDER_FAILURE_UNTIL.get(provider, 0.0) <= now,
                "retry_in_seconds": max(
                    0, round(_PROVIDER_FAILURE_UNTIL.get(provider, 0.0) - now)),
                "last_error": _PROVIDER_LAST_ERROR.get(provider),
            }
            for provider in providers
        }

# Standard open trackers appended to magnets built from a bare info-hash.
_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
]


@dataclass(frozen=True)
class TorrentResult:
    title: str
    magnet: str
    size_bytes: int
    seeders: int
    source: str        # "yts" | "tpb" | "nyaa" | "sukebei"
    media_type: str


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return resp.read()


def _magnet_from_hash(info_hash: str, name: str) -> str:
    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}"
    for tracker in _TRACKERS:
        magnet += f"&tr={urllib.parse.quote(tracker)}"
    return magnet


_BTIH_RE = re.compile(r"btih:([A-Za-z0-9]{32,40})", re.IGNORECASE)


def _infohash(magnet: str) -> str:
    """Lowercased btih info-hash from a magnet URI, or '' if absent. Used to
    dedupe pools across sources."""
    m = _BTIH_RE.search(magnet or "")
    return m.group(1).lower() if m else ""


# ---------------------------------------------------------------------------
# YTS — movies (JSON API)
# ---------------------------------------------------------------------------

def search_yts(query: str, *, limit: int = 20) -> list[TorrentResult]:
    if not _provider_available("yts"):
        logger.debug("YTS circuit open; skipping search for %r.", query)
        return []
    url = (
        "https://yts.mx/api/v2/list_movies.json?"
        + urllib.parse.urlencode({"query_term": query, "limit": limit, "sort_by": "seeds"})
    )
    try:
        payload = json.loads(_http_get(url))
    except Exception as exc:
        _record_provider_failure("yts", exc)
        return []
    _record_provider_success("yts")

    results: list[TorrentResult] = []
    for movie in (payload.get("data", {}).get("movies") or []):
        title = movie.get("title_long") or movie.get("title") or "?"
        for t in movie.get("torrents") or []:
            info_hash = t.get("hash")
            if not info_hash:
                continue
            quality = t.get("quality") or ""
            type_tag = t.get("type") or ""
            display = f"{title} [{quality} {type_tag}".strip() + " YTS]"
            results.append(TorrentResult(
                title=display,
                magnet=_magnet_from_hash(info_hash, display),
                size_bytes=int(t.get("size_bytes") or 0),
                seeders=int(t.get("seeds") or 0),
                source="yts",
                media_type="movie",
            ))
    return results


# ---------------------------------------------------------------------------
# The Pirate Bay — via the apibay.org JSON API (no HTML scraping)
# ---------------------------------------------------------------------------

def search_tpb(query: str, media_type: str, *, limit: int = 30,
               collect: bool = False) -> list[TorrentResult]:
    if not _provider_available("tpb"):
        logger.debug("TPB circuit open; skipping search for %r.", query)
        return []
    # cat=200 restricts to Video. (Categories: 201 movies, 205 TV, 207 HD
    # movies, 208 HD TV — 200 covers the whole video tree.)
    url = "https://apibay.org/q.php?" + urllib.parse.urlencode({"q": query, "cat": "200"})
    try:
        payload = json.loads(_http_get(url))
    except Exception as exc:
        _record_provider_failure("tpb", exc)
        return []
    _record_provider_success("tpb")

    results: list[TorrentResult] = []
    for item in payload if isinstance(payload, list) else []:
        info_hash = item.get("info_hash") or ""
        name = item.get("name") or ""
        # apibay returns a single placeholder row when there are no results
        if not info_hash or info_hash == "0000000000000000000000000000000000000000":
            continue
        results.append(TorrentResult(
            title=name,
            magnet=_magnet_from_hash(info_hash, name),
            size_bytes=int(item.get("size") or 0),
            seeders=int(item.get("seeders") or 0),
            source="tpb",
            media_type=media_type,
        ))
    # Collection mode keeps the API's own order and only BOUNDS the pool — it
    # must not seeder-sort-then-truncate, or a correct low-seed release gets
    # dropped before the gates ever run (section 4 item 1).
    if collect:
        return results[:limit]
    results.sort(key=lambda r: r.seeders, reverse=True)
    return results[:limit]


# ---------------------------------------------------------------------------
# nyaa.si / sukebei.nyaa.si — RSS feeds (carry infoHash, seeders, size)
# ---------------------------------------------------------------------------

_NYAA_NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
_SIZE_RE = re.compile(r"([\d.]+)\s*(TiB|GiB|MiB|KiB|B)", re.IGNORECASE)
_SIZE_MULT = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def _parse_nyaa_size(text: str) -> int:
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).lower()])


def _search_nyaa_rss(base: str, query: str, category: str, source: str,
                     media_type: str, *, limit: int = 30,
                     collect: bool = False) -> list[TorrentResult]:
    if not _provider_available(source):
        logger.debug("%s circuit open; skipping search for %r.", source, query)
        return []
    # RSS is sorted by seeders desc server-side. In collection mode we still
    # only BOUND the pool (take the first `limit`); the point is that `limit` is
    # the wide per-source pool, not a narrow final cut, and the selection gates —
    # not seeders — decide the winner (section 4 item 1).
    url = f"{base}/?" + urllib.parse.urlencode(
        {"page": "rss", "q": query, "c": category, "f": "0", "s": "seeders", "o": "desc"}
    )
    try:
        root = ET.fromstring(_http_get(url))
    except Exception as exc:
        _record_provider_failure(source, exc)
        return []
    _record_provider_success(source)

    results: list[TorrentResult] = []
    for item in root.iter("item"):
        title = item.findtext("title") or "?"
        info_hash = item.findtext("nyaa:infoHash", namespaces=_NYAA_NS) or ""
        seeders = int(item.findtext("nyaa:seeders", default="0", namespaces=_NYAA_NS) or 0)
        size = _parse_nyaa_size(item.findtext("nyaa:size", default="", namespaces=_NYAA_NS) or "")
        if not info_hash:
            continue
        results.append(TorrentResult(
            title=title,
            magnet=_magnet_from_hash(info_hash, title),
            size_bytes=size,
            seeders=seeders,
            source=source,
            media_type=media_type,
        ))
        if len(results) >= limit:
            break
    return results


def search_nyaa(query: str, *, limit: int = 30,
                collect: bool = False) -> list[TorrentResult]:
    # c=1_2: Anime — English-translated (same filter the old browser links used)
    return _search_nyaa_rss("https://nyaa.si", query, "1_2", "nyaa", "anime",
                            limit=limit, collect=collect)


def search_sukebei(query: str, *, limit: int = 30,
                   collect: bool = False) -> list[TorrentResult]:
    return _search_nyaa_rss("https://sukebei.nyaa.si", query, "0_0", "sukebei",
                            "xanime", limit=limit, collect=collect)


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def search_torrents(query: str, media_type: str, *, limit: int = 30) -> list[TorrentResult]:
    """Search the right source(s) for the media type, best-seeded first."""
    query = query.strip()
    if not query:
        return []

    results: list[TorrentResult] = []
    if media_type == "movie":
        results.extend(search_yts(query, limit=limit))
        results.extend(search_tpb(query, media_type, limit=limit))
    elif media_type == "anime":
        results.extend(search_nyaa(query, limit=limit))
    elif media_type == "xanime":
        results.extend(search_sukebei(query, limit=limit))
    else:  # tv / other / unknown
        results.extend(search_tpb(query, media_type, limit=limit))

    results.sort(key=lambda r: r.seeders, reverse=True)
    return results[:limit]


# ---------------------------------------------------------------------------
# Collection mode — wide per-source pools for the selection engine (Task B)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CollectedPool:
    """A widened, deduped candidate pool plus the per-source counts the
    selection decision records (pick_meta). `results` is NOT seeder-truncated —
    the selection gates, not seeders, decide the winner."""
    results: tuple           # tuple[TorrentResult, ...]
    pool_stats: dict         # {"per_source": {src: n}, "collected": n,
                             #  "deduped": n, "duplicates_removed": n}


def search_collect(query: str, media_type: str, *,
                   per_source: int = 50) -> CollectedPool:
    """Gather wide per-source pools (30-50 each), normalize, and dedupe by
    info-hash WITHOUT any global seeder truncation. This is what every automatic
    path uses instead of search_torrents (section 4 item 1). Per-source pool
    sizes are recorded for pick_meta.

    Automatic callers are NOT rewired to this yet — that is Phase 3. search_torrents
    keeps its legacy seeder-sorted shape for manual/legacy callers.
    """
    query = query.strip()
    per_source = max(1, min(int(per_source), 50))
    if not query:
        return CollectedPool(results=tuple(),
                             pool_stats={"per_source": {}, "collected": 0,
                                         "deduped": 0, "duplicates_removed": 0})

    per_source_results: list[tuple[str, list[TorrentResult]]] = []
    if media_type == "movie":
        per_source_results.append(("yts", search_yts(query, limit=per_source)))
        per_source_results.append(
            ("tpb", search_tpb(query, media_type, limit=per_source, collect=True)))
    elif media_type == "anime":
        per_source_results.append(
            ("nyaa", search_nyaa(query, limit=per_source, collect=True)))
    elif media_type == "xanime":
        per_source_results.append(
            ("sukebei", search_sukebei(query, limit=per_source, collect=True)))
    else:  # tv / other / unknown
        per_source_results.append(
            ("tpb", search_tpb(query, media_type, limit=per_source, collect=True)))

    per_source_stats: dict = {}
    collected: list[TorrentResult] = []
    for src, res in per_source_results:
        per_source_stats[src] = len(res)
        collected.extend(res)

    # Dedupe by info-hash. On a collision keep the copy reporting more seeders
    # (better swarm health for the identical payload); order is otherwise stable.
    seen: dict[str, int] = {}
    deduped: list[TorrentResult] = []
    for r in collected:
        ih = _infohash(r.magnet)
        key = ih or f"__noihash__{len(deduped)}"
        if key in seen:
            idx = seen[key]
            if r.seeders > deduped[idx].seeders:
                deduped[idx] = r
            continue
        seen[key] = len(deduped)
        deduped.append(r)

    stats = {
        "per_source": per_source_stats,
        "provider_health": provider_circuit_status(
            tuple(source for source, _results in per_source_results)),
        "collected": len(collected),
        "deduped": len(deduped),
        "duplicates_removed": len(collected) - len(deduped),
    }
    return CollectedPool(results=tuple(deduped), pool_stats=stats)


def format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size_bytes} B"
