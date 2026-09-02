# =============================================================================
# Phase 3 — every automatic decision path is wired into torrent_select.
#
# Each wired path gets at least one test proving it runs the engine (mock
# search sources), persists a selection_run with the right MODE and the chosen
# infohash, and that the cam_check injection catches an HDCAM-style name (here
# WORKPRINT) that RTN's ParsedData.trash alone would let through — the binding
# Phase 0/1 obligation.
# =============================================================================

import db
import downloads_store
import queue_store
import request_intake
import shows_store
import torrent_select as ts
from download_manager import DownloadManager
from media_lookup import MediaResult
from torrent_search import CollectedPool, TorrentResult

_MB = 1024 ** 2
_GB = 1024 ** 3


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _clean_db():
    for store in (queue_store, downloads_store, shows_store):
        pass
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    shows_store.initialize_shows_db()
    with db.connect() as conn:
        for table in ("requests", "downloads", "download_history",
                      "selection_runs", "candidate_decisions", "failed_grabs",
                      "grab_deferrals", "episodes", "show_folders",
                      "season_targets", "tracked_shows"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()


import pytest


@pytest.fixture(autouse=True)
def _fresh():
    _clean_db()
    yield
    _clean_db()


def _hash(seed: str) -> str:
    import hashlib
    return hashlib.sha1(seed.encode()).hexdigest()


def _res(title, seed, *, size=1400 * _MB, seeders=50, media_type="movie",
         source="tpb") -> TorrentResult:
    ih = _hash(seed)
    return TorrentResult(
        title=title, magnet=f"magnet:?xt=urn:btih:{ih}&dn=x",
        size_bytes=size, seeders=seeders, source=source, media_type=media_type)


def _pool(results):
    return CollectedPool(results=tuple(results),
                         pool_stats={"per_source": {"tpb": len(results)},
                                     "collected": len(results),
                                     "deduped": len(results),
                                     "duplicates_removed": 0})


def _movie(title="Inception", year=2010, ext="27205", countries=()):
    return MediaResult(
        title=title, year=year, external_id=ext,
        external_url=f"https://www.themoviedb.org/movie/{ext}",
        media_type="movie", overview="", source="tmdb",
        origin_countries=tuple(countries))


def _show(title="Test Drama", year=2014, ext="td-1", countries=("US",)):
    return MediaResult(
        title=title, year=year, external_id=ext,
        external_url=f"https://thetvdb.com/series/{ext}",
        media_type="tv", overview="", source="tvdb",
        origin_countries=tuple(countries))


def _dm(monkeypatch, results):
    dm = DownloadManager()
    monkeypatch.setattr("download_manager.search_collect",
                        lambda *a, **k: _pool(results))
    # No network in tests: the movie-runtime lookup is a live TMDB call.
    monkeypatch.setattr("download_manager._request_movie_minutes",
                        lambda *a, **k: None)
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    return dm


def _latest_run():
    with db.connect() as conn:
        row = conn.execute("SELECT MAX(id) FROM selection_runs").fetchone()
    return downloads_store.get_selection_run(row[0]) if row and row[0] else None


def _ihash(seed):  # the lowercased infohash the decision records
    return _hash(seed)


# ---------------------------------------------------------------------------
# Path 1 — movie / one-off requests: auto_grab_open_requests (automatic-single)
# ---------------------------------------------------------------------------

def test_movie_single_records_run_with_mode_and_infohash(monkeypatch):
    req = request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=_movie())
    winner = _res("Inception.2010.1080p.BluRay.x264-AMIABLE", "win",
                  size=1400 * _MB, seeders=40)
    dm = _dm(monkeypatch, [winner])

    started = dm.auto_grab_open_requests()
    assert len(started) == 1
    run = downloads_store.get_selection_run_for_download(started[0])
    assert run is not None
    assert run.mode == ts.MODE_AUTOMATIC_SINGLE
    assert run.chosen_infohash == _ihash("win")
    assert run.request_id == req.request_id


def test_movie_engine_rejects_sequel_even_with_more_seeders(monkeypatch):
    request_intake.add_matched_request(
        "The Angry Birds Movie", "cole", media_type="movie",
        match=_movie(title="The Angry Birds Movie", year=2016, ext="153518"))
    sequel = _res("The.Angry.Birds.Movie.2.2019.1080p.BluRay.x264-GRP", "seq",
                  size=2 * _GB, seeders=900)
    original = _res("The.Angry.Birds.Movie.2016.1080p.BluRay.x264-AMIABLE",
                    "orig", size=1400 * _MB, seeders=30)
    dm = _dm(monkeypatch, [sequel, original])

    started = dm.auto_grab_open_requests()
    assert len(started) == 1
    run = downloads_store.get_selection_run_for_download(started[0])
    assert run.chosen_infohash == _ihash("orig")  # sequel lost on the gate
    decisions = {d.infohash: d for d
                 in downloads_store.list_candidate_decisions(run.selection_run_id)}
    assert decisions[_ihash("seq")].passed is False
    assert decisions[_ihash("seq")].reason_code in (
        "sequel_mismatch", "numeric_title_mismatch")


# ---------------------------------------------------------------------------
# cam_check injection — WORKPRINT: RTN.trash misses it, is_cam_release catches
# ---------------------------------------------------------------------------

def test_workprint_passes_pure_engine_but_cam_check_injection_rejects():
    from video_quality import is_cam_release
    from torrent_select import Candidate, SelectWant, select_torrent
    from media_identity import MediaIdentity
    want = SelectWant(
        identity=MediaIdentity(media_type="movie", canonical_title="Inception",
                               canonical_year=2010),
        fallback_minutes=120.0)
    wp = Candidate("Inception.2010.WORKPRINT.x264-GRP", _hash("wp"),
                   1400 * _MB, 20)
    # Without the injected cam_check, RTN's trash flag alone lets WORKPRINT pass.
    d_no_check = select_torrent([wp], want)
    assert d_no_check.chosen_infohash == _hash("wp")
    # With cam_check=is_cam_release injected (as the wiring does), it is rejected.
    d_check = select_torrent([wp], want, cam_check=is_cam_release)
    assert not d_check.chosen
    assert d_check.verdicts[0].reason_code == "cam_or_trash"


def test_auto_grab_injects_cam_check_and_drops_workprint(monkeypatch):
    request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=_movie())
    wp = _res("Inception.2010.WORKPRINT.x264-GRP", "wp", seeders=999)
    clean = _res("Inception.2010.1080p.BluRay.x264-AMIABLE", "clean", seeders=10)
    dm = _dm(monkeypatch, [wp, clean])

    started = dm.auto_grab_open_requests()
    assert len(started) == 1
    run = downloads_store.get_selection_run_for_download(started[0])
    assert run.chosen_infohash == _ihash("clean")
    decisions = {d.infohash: d for d
                 in downloads_store.list_candidate_decisions(run.selection_run_id)}
    assert decisions[_ihash("wp")].reason_code == "cam_or_trash"


# ---------------------------------------------------------------------------
# Path 2 — season packs: _grab_season_pack (automatic-season-pack)
# ---------------------------------------------------------------------------

def test_season_pack_records_run_with_pack_mode(monkeypatch):
    req_row = request_intake.add_matched_request(
        "test drama", "cole", media_type="tv", match=_show(), season=1)
    req = queue_store.get_request(req_row.request_id)
    pack = _res("Test.Drama.US.S01.1080p.WEB.x264-GRP", "pack",
                size=6 * _GB, seeders=25, media_type="tv")
    dm = _dm(monkeypatch, [pack])

    started = dm._grab_request_seasonwise(req)
    assert len(started) == 1
    run = downloads_store.get_selection_run_for_download(started[0])
    assert run.mode == ts.MODE_AUTOMATIC_SEASON_PACK
    assert run.chosen_infohash == _ihash("pack")


def test_season_pack_rejects_wrong_country_edition(monkeypatch):
    req_row = request_intake.add_matched_request(
        "test drama", "cole", media_type="tv",
        match=_show(countries=("US",)), season=1)
    req = queue_store.get_request(req_row.request_id)
    au = _res("Test.Drama.AU.S01.1080p.WEB.x264-GRP", "au",
              size=6 * _GB, seeders=500, media_type="tv")
    dm = _dm(monkeypatch, [au])

    started = dm._grab_request_seasonwise(req)
    assert started == []  # AU contradicts the US want — nothing grabbed
    run = _latest_run()
    assert run is not None and run.mode == ts.MODE_AUTOMATIC_SEASON_PACK
    assert run.chosen_infohash is None


def test_max_and_ruby_pack_miss_enumerates_episodes(monkeypatch):
    """Live regression: an explicit TVDB season is not a pack-only request.

    The old worker repeatedly searched only ``Max & Ruby S01``, wrote a
    request-wide pack deferral, and never populated/traversed the episode grid.
    Prove the upgraded old hold is cleared, ``&``/``and`` are equivalent, E01's
    miss does not poison E02/E03, and every chosen episode keeps request
    provenance.
    """
    import maintenance
    import media_identity

    req = request_intake.add_matched_request(
        "Max and ruby", "cole", media_type="tv",
        match=_show(title="Max & Ruby", ext="71741", countries=()), season=1)
    show = shows_store.get_show_by_identity("tvdb", "71741")
    assert show is not None

    class _Ep:
        def __init__(self, episode):
            self.season = 1
            self.episode = episode
            self.title = f"Episode {episode}"
            self.air_date = f"2002-01-{episode:02d}"

    shows_store.replace_episodes(show.show_id, [_Ep(1), _Ep(2), _Ep(3)])
    downloads_store.set_grab_deferral(
        f"req:{req.request_id}", wait_hours=6,
        reason="no acceptable season pack found")
    queue_store.set_status(req.request_id, queue_store.STATUS_DEFERRED)

    queries = []

    def search(query, media_type):
        queries.append(query)
        # Force the provider-query ampersand fallback. E01 deliberately has no
        # release; E02 and E03 exist under the word "and" spelling.
        if query.startswith("Max and Ruby ") and "S01E02" in query:
            return _pool([_res(
                "Max.and.Ruby.S01E02.1080p.WEB.x264-GRP", "max-e02",
                size=400 * _MB, seeders=20, media_type="tv")])
        if query.startswith("Max and Ruby ") and "S01E03" in query:
            return _pool([_res(
                "Max.and.Ruby.S01E03.1080p.WEB.x264-GRP", "max-e03",
                size=400 * _MB, seeders=18, media_type="tv")])
        return _pool([])

    dm = _dm(monkeypatch, [])
    monkeypatch.setattr("download_manager.search_collect", search)
    monkeypatch.setattr(maintenance, "request_present_in_library",
                        lambda _req: False)

    started = dm.auto_grab_open_requests()

    assert len(started) == 2
    assert any(q == "Max & Ruby S01" for q in queries)
    assert any(q == "Max & Ruby S01E01" for q in queries)
    assert any(q == "Max and Ruby S01E01" for q in queries)
    assert any(q == "Max and Ruby S01E02" for q in queries)
    assert any(q == "Max and Ruby S01E03" for q in queries)
    assert media_identity.normalize_title("Max & Ruby") == (
        media_identity.normalize_title("Max and Ruby"))

    grabbed = [downloads_store.get_download(download_id)
               for download_id in started]
    assert {(row.season, row.episode) for row in grabbed if row is not None} == {
        (1, 2), (1, 3)}
    assert all(row.request_id == req.request_id
               for row in grabbed if row is not None)
    assert downloads_store.get_grab_deferral(
        f"ep:{show.show_id}:1:1") is not None
    assert downloads_store.get_grab_deferral(f"req:{req.request_id}") is None


def test_season_episode_sync_contention_does_not_defer_request(monkeypatch):
    """A concurrent Shows pass is a five-minute retry, not a six-hour miss."""
    import show_tracker

    req = request_intake.add_matched_request(
        "Busy Show", "cole", media_type="tv",
        match=_show(title="Busy Show", ext="busy-1", countries=()), season=1)
    dm = _dm(monkeypatch, [])
    monkeypatch.setattr(
        show_tracker, "sync_show",
        lambda *_a, **_k: (_ for _ in ()).throw(
            show_tracker.ShowsBusyError("another Shows pass owns the lock")))

    assert dm._grab_request_seasonwise(req) == []
    refreshed = queue_store.get_request(req.request_id)
    assert refreshed is not None and refreshed.status == queue_store.STATUS_OPEN
    assert downloads_store.get_grab_deferral(f"req:{req.request_id}") is None


# ---------------------------------------------------------------------------
# Path 3 — episodes: _grab_one_episode (automatic-episode + request linkage)
# ---------------------------------------------------------------------------

def _tracked_show(**kw):
    show_id = shows_store.upsert_show(
        title=kw.get("title", "Test Show"), media_type="tv",
        source="tmdb", external_id=kw.get("ext", "99"))
    return shows_store.get_show(show_id)


def _episode(show_id, season=1, episode=3):
    return shows_store.EpisodeRow(
        episode_id=1, show_id=show_id, season=season, episode=episode,
        title="", air_date="2020-01-01", has_file=False, file_path=None,
        grab_download_id=None)


def test_episode_grab_records_run_and_links_request(monkeypatch):
    show = _tracked_show()
    ep = _episode(show.show_id)
    req = request_intake.add_matched_request(
        "test show", "cole", media_type="tv", match=_show(
            title="Test Show", ext="99", countries=()), season=1)
    cand = _res("Test.Show.S01E03.1080p.WEB.x264-GRP", "ep",
                size=400 * _MB, seeders=15, media_type="tv")
    dm = _dm(monkeypatch, [cand])

    started = dm._grab_one_episode(show, ep, request_id=req.request_id)
    assert len(started) == 1
    run = downloads_store.get_selection_run_for_download(started[0])
    assert run.mode == ts.MODE_AUTOMATIC_EPISODE
    assert run.chosen_infohash == _ihash("ep")
    # The provenance leak is closed: the download carries its originating request.
    dl = downloads_store.get_download(started[0])
    assert dl.request_id == req.request_id


# ---------------------------------------------------------------------------
# Path 4 — follow-new / keep-at-100 pass routes through the engine too
# (auto_grab_missing -> _grab_one_episode, the same engine-wired function)
# ---------------------------------------------------------------------------

def test_follow_new_keep_at_100_pass_routes_through_engine(monkeypatch):
    import show_tracker
    show = _tracked_show(title="Kept Show", ext="kept-1")

    class _Ep:
        def __init__(self, s, e):
            self.season, self.episode, self.title, self.air_date = s, e, "", "2020-01-01"

    shows_store.replace_episodes(show.show_id, [_Ep(1, 1)])
    monkeypatch.setattr(show_tracker, "sync_show", lambda *a, **k: None)
    cand = _res("Kept.Show.S01E01.1080p.WEB.x264-GRP", "kept",
                size=400 * _MB, seeders=12, media_type="tv")
    dm = _dm(monkeypatch, [cand])

    # Explicit show selection bypasses the auto_grab flag but hits the same
    # missing-episode -> _grab_one_episode -> engine path.
    started = dm._auto_grab_missing_impl(limit=5, show_ids=[show.show_id])
    assert len(started) == 1
    run = downloads_store.get_selection_run_for_download(started[0])
    assert run.mode == ts.MODE_AUTOMATIC_EPISODE
    assert run.chosen_infohash == _ihash("kept")


# ---------------------------------------------------------------------------
# Path 5 — replacement: replace_low_quality_movie (automatic-replacement)
# ---------------------------------------------------------------------------

def test_replacement_records_run_with_replacement_mode(monkeypatch):
    clean = _res("Some.Movie.2015.1080p.BluRay.x264-GRP", "repl",
                 size=3 * _GB, seeders=20)
    dm = _dm(monkeypatch, [clean])
    # old_path need not exist (ffprobe is wrapped in try/except OSError).
    did = dm.replace_low_quality_movie("Some Movie", "C:/nope/old.mkv")
    assert did is not None
    run = downloads_store.get_selection_run_for_download(did)
    assert run.mode == ts.MODE_AUTOMATIC_REPLACEMENT
    assert run.chosen_infohash == _ihash("repl")


def test_replacement_never_takes_a_cam(monkeypatch):
    cam = _res("Some.Movie.2015.HDCAM.x264-GRP", "cam", size=3 * _GB, seeders=999)
    dm = _dm(monkeypatch, [cam])
    did = dm.replace_low_quality_movie("Some Movie", "C:/nope/old.mkv")
    assert did is None  # a cam is exactly what replacement must never grab
    run = _latest_run()
    assert run is not None and run.mode == ts.MODE_AUTOMATIC_REPLACEMENT
    assert run.chosen_infohash is None


# ---------------------------------------------------------------------------
# Path 6 — zero-seeder race: candidates pass gates 1-8 (zero-seeder-race)
# ---------------------------------------------------------------------------

def test_zero_seeder_race_gates_candidates_and_records_run(monkeypatch):
    req = request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=_movie())
    good = _res("Inception.2010.1080p.WEB.x264-GRP", "zgood", seeders=0)
    cam = _res("Inception.2010.WORKPRINT.x264-GRP", "zcam", seeders=0)
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(dm, "_race_monitor", lambda *a, **k: None)

    ids = dm.start_zero_seeder_race(
        [good, cam], "movie", request_id=req.request_id,
        request_title="Inception 2010", minutes=120)
    # Only the gate survivor is raced; the WORKPRINT candidate is dropped.
    assert len(ids) == 1
    run = _latest_run()
    assert run is not None and run.mode == ts.MODE_ZERO_SEEDER_RACE
    decisions = {d.infohash: d for d
                 in downloads_store.list_candidate_decisions(run.selection_run_id)}
    assert decisions[_ihash("zcam")].reason_code == "cam_or_trash"
    assert decisions[_ihash("zgood")].passed is True


# ---------------------------------------------------------------------------
# Path 7 — manual picks: manual-user-pick preflight + typed override
# ---------------------------------------------------------------------------

def test_manual_clean_pick_passes_preflight(monkeypatch):
    req = request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=_movie())
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    good = _res("Inception.2010.1080p.BluRay.x264-GRP", "mgood")

    outcome = dm.manual_grab(good, request_id=req.request_id,
                             request_title="Inception 2010")
    assert outcome.ok and outcome.download_id is not None
    assert not outcome.needs_override
    run = downloads_store.get_selection_run(outcome.selection_run_id)
    assert run.mode == ts.MODE_MANUAL_USER_PICK
    assert run.chosen_infohash == _ihash("mgood")


def test_manual_sequel_pick_needs_override_then_records_it(monkeypatch):
    req = request_intake.add_matched_request(
        "The Angry Birds Movie", "cole", media_type="movie",
        match=_movie(title="The Angry Birds Movie", year=2016, ext="153518"))
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    sequel = _res("The.Angry.Birds.Movie.2.2019.1080p.BluRay.x264-GRP", "mseq")

    blocked = dm.manual_grab(sequel, request_id=req.request_id,
                             request_title="The Angry Birds Movie 2016")
    assert blocked.ok is False and blocked.needs_override
    assert blocked.download_id is None
    assert blocked.reason_code in ("sequel_mismatch", "numeric_title_mismatch")

    forced = dm.manual_grab(sequel, request_id=req.request_id,
                            request_title="The Angry Birds Movie 2016",
                            override_reason="cole says grab it anyway")
    assert forced.ok and forced.download_id is not None
    run = downloads_store.get_selection_run(forced.selection_run_id)
    assert run.mode == ts.MODE_MANUAL_USER_PICK
    import json
    stats = json.loads(run.pool_stats_json)
    assert stats.get("manual_override") == "cole says grab it anyway"
    assert stats.get("overridden_reason_code") in (
        "sequel_mismatch", "numeric_title_mismatch")
