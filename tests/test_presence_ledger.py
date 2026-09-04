# =============================================================================
# tests/test_presence_ledger.py
# =============================================================================
# The post-placement loop Cole asked for:
#
#   register -> reap -> settle
#
# 1. a placement puts every episode it supplied on the master list, per
#    episode, INCLUDING every file inside a season pack, and does it whether
#    or not the download carried show/request context
# 2. downloads still in flight for episodes that placement supplied are
#    cancelled — and the placement can never cancel itself
# 3. a movie request completes on its file; a season request completes when
#    the aired set is complete, including when the placement was never linked
#    to that request
# 4. the user can ask for the exact state of a request, file by file
# =============================================================================

from dataclasses import dataclass

import pytest

import downloads_store
import presence_ledger
import shows_store

_MB = 1024 * 1024
_GB = 1024 * _MB


@dataclass(frozen=True)
class _Ep:
    """Duck-typed episode for shows_store.replace_episodes."""
    season: int
    episode: int
    title: str
    air_date: str | None


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "ledger.db")
    downloads_store.initialize_downloads_db()
    shows_store.initialize_shows_db()
    yield


def _show(title="Widow's Bay", episodes=10, season=1, aired=True):
    show_id = shows_store.upsert_show(
        title=title, media_type="tv", source="tvdb", external_id="454109")
    shows_store.replace_episodes(show_id, [
        _Ep(season=season, episode=i, title=f"E{i}",
            air_date="2026-01-01" if aired else "2099-01-01")
        for i in range(1, episodes + 1)
    ])
    return shows_store.get_show(show_id)


def _download(*, title, show_id=None, season=None, episode=None,
              request_id=None, status="moved", media_type="tv"):
    download_id = downloads_store.create_download(
        title=title, magnet=f"magnet:?xt=urn:btih:{abs(hash(title)):040x}",
        source="tpb", media_type=media_type, request_id=request_id,
        staging_dir="C:/staging", planned_dest=None, planned_name=None,
        route_reason="test", auto_rename=False, auto_move=False,
        show_id=show_id, season=season, episode=episode)
    downloads_store.set_status(download_id, status)
    return downloads_store.get_download(download_id)


def _place_files(download_id, pairs, *, folder="I:/tv/Widows Bay/Season 01"):
    """Record verified placed files for (season, episode) pairs."""
    for season, episode in pairs:
        downloads_store.add_download_file(
            download_id,
            source_relative_path=f"S{season:02d}E{episode:02d}.mkv",
            source_absolute_path=None, media_role="video",
            parsed_season=season, parsed_episode=episode,
            final_path=f"{folder}/Widows.Bay.S{season:02d}E{episode:02d}.mkv",
            verification_state="verified", size_bytes=_GB)


# ---------------------------------------------------------------------------
# 1. register
# ---------------------------------------------------------------------------

def test_season_pack_registers_every_episode_inside_it():
    show = _show()
    row = _download(title="Widows.Bay.S01.Complete", show_id=show.show_id,
                    season=1)
    _place_files(row.download_id, [(1, i) for i in range(1, 11)])

    reg = presence_ledger.register_download(row)

    assert sorted(reg.episodes) == list(range(1, 11))
    grid = [e for e in shows_store.list_episodes(show.show_id) if e.season == 1]
    assert all(e.has_file for e in grid)


def test_registration_works_with_no_show_context(monkeypatch):
    """The Widow's Bay case: a hand-grabbed pack, no show_id, no request_id."""
    show = _show()
    row = _download(title="Widows.Bay.S01.Complete.1080p-NeoNoir")
    assert row.show_id is None and row.request_id is None
    _place_files(row.download_id, [(1, i) for i in range(1, 11)])

    reg = presence_ledger.register_download(
        row, resolve_show=lambda _row: show)

    assert reg.show_id == show.show_id
    assert len(reg.episodes) == 10
    assert all(e.has_file for e in shows_store.list_episodes(show.show_id))


def test_episode_numbers_are_recovered_from_the_placed_filename():
    """A pack whose files were named at move time carries no parsed_episode."""
    show = _show(episodes=2)
    row = _download(title="pack", show_id=show.show_id, season=1)
    downloads_store.add_download_file(
        row.download_id, source_relative_path="a.mkv",
        source_absolute_path=None, media_role="video",
        final_path="I:/tv/Widows Bay/Season 01/Widows.Bay.S01E02.Lodging.mkv",
        verification_state="verified")

    reg = presence_ledger.register_download(row)

    assert reg.episodes == (2,)


def test_unverified_files_are_not_evidence():
    show = _show(episodes=3)
    row = _download(title="pack", show_id=show.show_id, season=1)
    downloads_store.add_download_file(
        row.download_id, source_relative_path="a.mkv",
        source_absolute_path=None, media_role="video",
        parsed_season=1, parsed_episode=1, final_path="I:/tv/x/E01.mkv",
        verification_state="failed")

    assert presence_ledger.register_download(row).episodes == ()


# ---------------------------------------------------------------------------
# 2. reap
# ---------------------------------------------------------------------------

def test_placement_cancels_in_flight_downloads_it_made_redundant():
    show = _show()
    pack = _download(title="Widows.Bay.S01.Complete", show_id=show.show_id,
                     season=1)
    _place_files(pack.download_id, [(1, i) for i in range(1, 11)])
    singles = [_download(title=f"Widows Bay S01E{i:02d}", show_id=show.show_id,
                         season=1, episode=i, status="queued")
               for i in range(1, 11)]

    reg = presence_ledger.register_download(pack)
    reaped = presence_ledger.reap_redundant(pack, reg)

    assert sorted(reaped) == sorted(s.download_id for s in singles)
    for single in singles:
        row = downloads_store.get_download(single.download_id)
        assert row.status == "cancelled"
        assert row.removed_at is not None


def test_reaper_never_cancels_the_download_that_triggered_it():
    show = _show(episodes=2)
    pack = _download(title="pack", show_id=show.show_id, season=1,
                     status="downloading")
    _place_files(pack.download_id, [(1, 1), (1, 2)])

    reg = presence_ledger.register_download(pack)
    reaped = presence_ledger.reap_redundant(pack, reg)

    assert pack.download_id not in reaped
    assert downloads_store.get_download(pack.download_id).removed_at is None


def test_reaper_leaves_other_shows_and_seasons_alone():
    show = _show()
    other = shows_store.upsert_show(title="Other", media_type="tv",
                                    source="tvdb", external_id="999")
    pack = _download(title="pack", show_id=show.show_id, season=1)
    _place_files(pack.download_id, [(1, 1)])
    keep_season = _download(title="s2e1", show_id=show.show_id, season=2,
                            episode=1, status="queued")
    keep_show = _download(title="other s1e1", show_id=other, season=1,
                          episode=1, status="queued")

    reg = presence_ledger.register_download(pack)
    reaped = presence_ledger.reap_redundant(pack, reg)

    assert keep_season.download_id not in reaped
    assert keep_show.download_id not in reaped


def test_reaper_leaves_finished_and_failed_rows_alone():
    show = _show(episodes=1)
    pack = _download(title="pack", show_id=show.show_id, season=1)
    _place_files(pack.download_id, [(1, 1)])
    done = _download(title="already moved", show_id=show.show_id, season=1,
                     episode=1, status="moved")
    dead = _download(title="failed", show_id=show.show_id, season=1,
                     episode=1, status="error")

    reg = presence_ledger.register_download(pack)
    reaped = presence_ledger.reap_redundant(pack, reg)

    assert done.download_id not in reaped
    assert dead.download_id not in reaped


# ---------------------------------------------------------------------------
# 4. report
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, **kw):
        self.request_id = kw.get("request_id", 1)
        self.resolved_title = kw.get("resolved_title", "Widow's Bay")
        self.content = kw.get("content", "widows bay")
        self.media_type = kw.get("media_type", "tv")
        self.status = kw.get("status", "grabbing")
        self.season = kw.get("season", 1)


def test_report_states_every_episode_of_a_season_request():
    show = _show(episodes=4)
    shows_store.set_episode_file(show.show_id, 1, 1, "I:/tv/e1.mkv")
    _download(title="e2", show_id=show.show_id, season=1, episode=2,
              request_id=1, status="downloading")

    report = presence_ledger.request_report(
        1, get_request=lambda _id: _Req(), resolve_show=lambda _req: show)

    states = {(f.season, f.episode): f.state for f in report.files}
    assert states[(1, 1)] == "on-disk"
    assert states[(1, 2)] == "downloading"
    assert states[(1, 3)] == "missing"
    assert "1 on-disk" in report.summary


def test_report_marks_unaired_episodes_separately():
    show = _show(episodes=2, aired=False)

    report = presence_ledger.request_report(
        1, get_request=lambda _id: _Req(), resolve_show=lambda _req: show)

    assert {f.state for f in report.files} == {"unaired"}
    assert "unaired" in report.summary


def test_report_for_a_movie_lists_its_files():
    row = _download(title="A Film 2024 1080p", request_id=7, media_type="movie",
                    status="moved")
    downloads_store.add_download_file(
        row.download_id, source_relative_path="film.mkv",
        source_absolute_path=None, media_role="video",
        final_path="I:/movies/A Film (2024)/A Film (2024).mkv",
        verification_state="verified", size_bytes=2 * _GB)

    report = presence_ledger.request_report(
        7, get_request=lambda _id: _Req(request_id=7, media_type="movie",
                                        season=None))

    assert len(report.files) == 1
    assert report.files[0].state == "on-disk"
    assert report.files[0].title == "A Film (2024).mkv"


def test_report_is_none_for_an_unknown_request():
    assert presence_ledger.request_report(
        999, get_request=lambda _id: None) is None


# ---------------------------------------------------------------------------
# 0. the info-hash guard — the same bytes are never fetched twice
# ---------------------------------------------------------------------------

def test_live_download_for_finds_an_in_flight_copy_of_the_same_payload():
    row = _download(title="A Film", media_type="movie", status="downloading")
    assert presence_ledger.live_download_for(row.magnet).download_id == row.download_id


def test_live_download_for_can_exclude_itself():
    row = _download(title="A Film", media_type="movie", status="downloading")
    assert presence_ledger.live_download_for(
        row.magnet, exclude=row.download_id) is None


def test_finished_and_archived_copies_do_not_block_a_fresh_grab():
    done = _download(title="A Film", media_type="movie", status="moved")
    assert presence_ledger.live_download_for(done.magnet) is None

    dead = _download(title="B Film", media_type="movie", status="downloading")
    downloads_store.tombstone_download(dead.download_id, reason="archived")
    assert presence_ledger.live_download_for(dead.magnet) is None


def test_a_magnet_with_no_infohash_never_matches():
    assert presence_ledger.live_download_for("magnet:?dn=nothing") is None
    assert presence_ledger.live_download_for("") is None


# ---------------------------------------------------------------------------
# The admin naming an unroutable download
# ---------------------------------------------------------------------------

def test_admin_name_is_remembered_and_outranks_derived_titles():
    """Routing refuses to invent a folder from a release name, so a human
    naming it is the only thing that unblocks it. That name must stick."""
    import download_manager

    row = _download(title="Her Blaze S01E01 2026 1080p WEB-DL H 265 AAC-ADWeb",
                    status="downloading")
    assert row.route_title is None

    downloads_store.set_route_title(row.download_id, "Her Blaze")
    named = downloads_store.get_download(row.download_id)

    assert named.route_title == "Her Blaze"
    assert download_manager._request_title_from_row(named) == "Her Blaze"


def test_naming_a_download_still_in_flight_defers_instead_of_failing():
    """The row is not 'downloaded' yet, so the route cannot be applied now —
    it must be recorded for completion, not rejected."""
    import download_manager

    row = _download(title="Her Blaze S01E01", status="downloading")
    manager = download_manager.DownloadManager.__new__(
        download_manager.DownloadManager)
    outcome = download_manager.DownloadManager.apply_route(
        manager, row.download_id, request_title="Her Blaze")

    assert "Her Blaze" in outcome
    assert downloads_store.get_download(row.download_id).route_title == "Her Blaze"


def test_naming_creates_the_folder_a_release_name_could_not(tmp_path, monkeypatch):
    """With a canonical name the router creates a new show folder; with only
    the release name it refuses. That refusal is the guard, not a bug: it is
    what stops "Her Blaze", "Her.Blaze" and "Her Blaze 2026" all becoming
    separate library folders."""
    import torrent_routing

    tv_root = tmp_path / "Tv Shows"
    tv_root.mkdir()
    monkeypatch.setattr(torrent_routing.config, "media_paths_for_types",
                        lambda _mt: [str(tv_root)], raising=False)
    monkeypatch.setattr(torrent_routing, "find_show_folder",
                        lambda *a, **k: (None, 0.0, None))

    release = "Her Blaze S01E01 2026 1080p WEB-DL H 265 AAC-ADWeb"

    unnamed = torrent_routing.plan_route(release, "tv")
    assert not unnamed.confident
    assert "no confident show-folder match" in unnamed.reason

    named = torrent_routing.plan_route(release, "tv", request_title="Her Blaze")
    assert named.confident
    assert "Her Blaze" in named.show_folder
    assert named.season_folder == "Season 01"
