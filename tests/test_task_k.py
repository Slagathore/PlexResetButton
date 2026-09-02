# =============================================================================
# tests/test_task_k.py  — Folder watcher + complete downtime caching (Task K)
# =============================================================================
# Covers, per the sprint spec's item 5:
#   - the settle debouncer as a pure unit (quiet window + size stability),
#     driven with injected clock/size probes so there is no real sleep or disk;
#   - the SCOPED index delta, including that the collapse guard is judged
#     against the changed subtree and never the whole index (both directions:
#     a tiny legit change can't trip it, a large per-subtree vanish still does);
#   - request auto-close when a file appears — reconcile_requests_with_library
#     directly, and the whole watcher pipeline (index -> offline identity ->
#     reconcile) end to end on a real tmp folder, offline;
#   - the overnight idle-pass additions and the watcher lifecycle, asserted at
#     source level (the sprint rules forbid launching the Tk GUI — same
#     technique as tests/test_maint_wiring.py);
#   - real watchdog observers on a tmp dir, plus the graceful-off paths.
#
# No network: the pipeline/reconcile tests resolve identity offline (episode /
# show-folder inheritance) and via the identity join — conftest's socket guard
# would fail instantly if a real call slipped through.
# =============================================================================
import os
import sqlite3
import threading
import time

import pytest

import config
import db
import library_index
import library_watch


# ---------------------------------------------------------------------------
# Shared-DB helpers (the conftest temp app DB; every store points at it).
# ---------------------------------------------------------------------------

def _clear_shared() -> None:
    import library_identity as li
    import queue_store as qs
    library_index.initialize_library_index_db()
    li.initialize_library_identity_db()
    qs.initialize_queue_db()
    with db.connect() as conn:
        conn.execute("DELETE FROM library_files")
        conn.execute("DELETE FROM library_identity")
        conn.execute("DELETE FROM requests")
        conn.commit()


def _index(paths_names: list[tuple[str, str]]) -> None:
    library_index.initialize_library_index_db()
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO library_files "
            "(path, name, root_path, search_name, size_bytes, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(p, n, "root", n.casefold(), 1, 0.0) for p, n in paths_names])
        conn.commit()


# ---------------------------------------------------------------------------
# 1. Debouncer — pure unit (quiet + size stable), injected clock/probe.
# ---------------------------------------------------------------------------

def test_debouncer_holds_until_quiet():
    t = {"v": 0.0}
    sizes = {"/lib/a.mkv": 100}
    deb = library_watch.SettleDebouncer(
        60, clock=lambda: t["v"], size_probe=lambda p: sizes.get(p))
    deb.note("/lib/a.mkv")
    assert deb.pending() == {"/lib/a.mkv"}
    assert not deb.ready()          # t=0, just noted
    t["v"] = 59
    assert not deb.ready()          # inside the settle window
    t["v"] = 61
    assert deb.ready()              # quiet AND size-stable
    assert deb.drain() == {"/lib/a.mkv"}
    assert deb.pending() == set()
    assert not deb.ready()          # drained -> nothing pending


def test_debouncer_waits_for_size_stable():
    """A file that grew since its last sample is still being written: the batch
    stays NOT ready and the quiet window re-opens until the copy finishes."""
    t = {"v": 0.0}
    sizes = {"/lib/big.mkv": 1_000}
    deb = library_watch.SettleDebouncer(
        60, clock=lambda: t["v"], size_probe=lambda p: sizes.get(p))
    deb.note("/lib/big.mkv")
    t["v"] = 61
    sizes["/lib/big.mkv"] = 5_000   # grew after the last note -> unstable
    assert not deb.ready()          # quiet, but not size-stable -> hold
    t["v"] = 130                    # >= 61 (re-opened) + 60, size unchanged now
    assert deb.ready()


def test_debouncer_empty_is_never_ready():
    deb = library_watch.SettleDebouncer(60, clock=lambda: 10_000.0)
    assert not deb.ready()


# ---------------------------------------------------------------------------
# 2. Scoped index delta + guard scoping.
# ---------------------------------------------------------------------------

@pytest.fixture
def index_db(tmp_path, monkeypatch):
    db_file = tmp_path / "k_index.db"
    monkeypatch.setattr(library_index, "_db_path", lambda: db_file)
    monkeypatch.setattr(config, "LIBRARY_INDEX_EXTENSIONS", [".mkv"])
    library_index.initialize_library_index_db()
    return db_file


def _seed(db_file, rows: list[tuple[str, str, str]]) -> None:
    with sqlite3.connect(db_file) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO library_files "
            "(path, name, root_path, search_name, size_bytes, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(p, n, r, n.casefold(), 10, 0.0) for p, n, r in rows])
        conn.commit()


def _paths(db_file) -> set[str]:
    with sqlite3.connect(db_file) as conn:
        return {r[0] for r in conn.execute("SELECT path FROM library_files")}


def _count(db_file) -> int:
    with sqlite3.connect(db_file) as conn:
        return conn.execute("SELECT COUNT(*) FROM library_files").fetchone()[0]


def test_scoped_refresh_only_touches_scope(index_db, tmp_path, monkeypatch):
    """A scoped delta adds a new file under the scope and leaves rows OUTSIDE
    the scope untouched — even one that is now missing on disk (proving removals
    never spill past the scope)."""
    root = tmp_path / "media"
    (root / "showA").mkdir(parents=True)
    (root / "showB").mkdir(parents=True)
    a1 = root / "showA" / "ep1.mkv"
    a1.write_bytes(b"x" * 10)
    a2 = root / "showA" / "ep2.mkv"        # new on disk, not indexed yet
    a2.write_bytes(b"x" * 20)
    ghost = root / "showB" / "ghost.mkv"   # indexed, NOT on disk, out of scope
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])
    _seed(index_db, [(str(a1), "ep1.mkv", str(root)),
                     (str(ghost), "ghost.mkv", str(root))])

    result = library_index.refresh_library_index(scope=[str(root / "showA")])

    assert not result.aborted_reason
    assert result.added == 1               # a2 picked up
    assert result.removed == 0             # ghost NOT removed (out of scope)
    paths = _paths(index_db)
    assert str(a2) in paths
    assert str(ghost) in paths             # preserved, untouched


def test_scoped_refresh_guard_trips_on_scope_collapse(index_db, tmp_path, monkeypatch):
    """A scoped pass whose subtree collapsed (>=100 rows, ~none on disk) is
    refused, even though the rest of the (out-of-scope) index is healthy. The
    guard is judged against the SCOPE — so a large per-subtree vanish still
    trips it instead of silently wiping those rows. A global-count guard would
    NOT trip here (survivors stay high) and would wrongly wipe the subtree."""
    root = tmp_path / "media"
    sub = root / "bigshow"
    sub.mkdir(parents=True)                # exists, EMPTY on disk
    (root / "other").mkdir()
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])
    rows = [(str(sub / f"ep{i:04d}.mkv"), f"ep{i:04d}.mkv", str(root))
            for i in range(150)]
    rows += [(str(root / "other" / f"o{i:04d}.mkv"), f"o{i:04d}.mkv", str(root))
             for i in range(400)]          # healthy, out of scope
    _seed(index_db, rows)

    result = library_index.refresh_library_index(scope=[str(sub)])

    assert result.aborted_reason
    assert _count(index_db) == 550         # nothing wiped


def test_scoped_refresh_guard_ignores_whole_index_size(index_db, tmp_path, monkeypatch):
    """The inverse: a small legitimate scoped removal must NOT trip the guard
    just because the whole index is large. The guard sees only the scope (1
    row), so 1 < 100 -> guard off, the removal applies."""
    root = tmp_path / "media"
    (root / "small").mkdir(parents=True)   # exists, empty on disk
    (root / "other").mkdir()
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])
    rows = [(str(root / "other" / f"o{i:04d}.mkv"), f"o{i:04d}.mkv", str(root))
            for i in range(200)]
    rows.append((str(root / "small" / "gone.mkv"), "gone.mkv", str(root)))
    _seed(index_db, rows)

    result = library_index.refresh_library_index(scope=[str(root / "small")])

    assert not result.aborted_reason
    assert result.removed == 1
    assert _count(index_db) == 200


def test_scoped_refresh_ignores_scope_outside_configured_roots(index_db, tmp_path, monkeypatch):
    """A scope path that isn't under any configured root is skipped, never used
    to remove rows (a stray event outside the library can't wipe anything)."""
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])
    _seed(index_db, [(str(root / "keep.mkv"), "keep.mkv", str(root))])

    result = library_index.refresh_library_index(scope=[str(outside)])

    assert not result.aborted_reason
    assert result.added == 0 and result.removed == 0
    assert str(root / "keep.mkv") in _paths(index_db)


def test_indexed_paths_under_prefix_match(index_db, monkeypatch):
    _seed(index_db, [
        (r"C:\Media\ShowA\ep1.mkv", "ep1.mkv", r"C:\Media"),
        (r"C:\Media\ShowB\ep1.mkv", "ep1.mkv", r"C:\Media"),
    ])
    under = library_index.indexed_paths_under([r"C:\Media\ShowA"])
    assert under == [r"C:\Media\ShowA\ep1.mkv"]
    # Stored Windows paths keep Windows case/separator semantics even when
    # this test runs on a Linux CI host.
    assert library_index.indexed_paths_under(["c:/MEDIA/showa"]) == under
    assert library_index.indexed_paths_under([]) == []


# ---------------------------------------------------------------------------
# 3. Request auto-close (reconcile_requests_with_library).
# ---------------------------------------------------------------------------

def test_reconcile_closes_open_and_grabbing_when_present():
    """Both an OPEN and a GRABBING request whose exact identity is now in the
    library close as fulfilled — the admin (or our own pipeline) beat us to it."""
    _clear_shared()
    import library_identity as li
    import maintenance
    import queue_store as qs
    path = "/lib/Dune (2021).mkv"
    _index([(path, "Dune (2021).mkv")])
    li.set_identity(path, media_type="movie", identity_source="tmdb",
                    external_id="438631", resolved_by=li.RESOLVED_DOWNLOAD,
                    canonical_title="Dune", canonical_year=2021)
    open_req = qs.add_request("Dune", "t", media_type="movie",
                              resolved_title="Dune", external_id="438631",
                              identity_source="tmdb", canonical_year=2021,
                              status=qs.STATUS_OPEN)
    grab_req = qs.add_request("Dune", "t", media_type="movie",
                              resolved_title="Dune", external_id="438631",
                              identity_source="tmdb", canonical_year=2021,
                              status=qs.STATUS_GRABBING)

    out = maintenance.reconcile_requests_with_library()

    assert qs.get_request(open_req.request_id).status == qs.STATUS_FULFILLED
    assert qs.get_request(grab_req.request_id).status == qs.STATUS_FULFILLED
    assert out["closed"] >= 2


def test_reconcile_marks_season_found_but_keeps_it_open():
    """A season-specific request that is present is marked found but NOT closed
    — that is _closeable_on_library_match's rule, reused verbatim (found proves
    the show is present, not that the season is complete)."""
    _clear_shared()
    import library_identity as li
    import maintenance
    import queue_store as qs
    path = "/lib/Show/S01E01.mkv"
    _index([(path, "S01E01.mkv")])
    li.set_identity(path, media_type="tv", identity_source="tvdb",
                    external_id="42", season=1, episode=1,
                    resolved_by=li.RESOLVED_EPISODE, canonical_title="Show")
    req = qs.add_request("Show S1", "t", media_type="tv", resolved_title="Show",
                         external_id="42", identity_source="tvdb", season=1,
                         status=qs.STATUS_OPEN)

    maintenance.reconcile_requests_with_library()

    row = qs.get_request(req.request_id)
    assert row.found_in_library            # present -> found flag set
    assert row.status == qs.STATUS_OPEN    # season-specific -> not closed


# ---------------------------------------------------------------------------
# 3b. backfill_paths scoping (offline identity for just the new files).
# ---------------------------------------------------------------------------

def test_backfill_paths_only_targets_given_paths(tmp_path):
    _clear_shared()
    import library_identity as li
    import shows_store
    show_id = shows_store.upsert_show(title="Zed", media_type="tv",
                                      source="tvdb", external_id="88", year=2001)
    folder = tmp_path / "Zed"
    a = str(folder / "Season 01" / "Zed S01E01.mkv")
    b = str(folder / "Season 01" / "Zed S01E02.mkv")
    shows_store.add_show_folder(show_id, str(folder))
    _index([(a, "Zed S01E01.mkv"), (b, "Zed S01E02.mkv")])

    summary = li.backfill_paths([a])       # resolve ONLY a

    assert summary["show_folder"] >= 1
    assert li.get_identity(a).external_id == "88"
    assert li.get_identity(b) is None      # b left unidentified


# ---------------------------------------------------------------------------
# 4. Full watcher pipeline end to end: a new file appears -> index -> offline
#    identity -> the matching request auto-closes. Offline (show-folder
#    inheritance), on a real tmp folder.
# ---------------------------------------------------------------------------

def test_process_settled_paths_indexes_identifies_and_closes(tmp_path, monkeypatch):
    _clear_shared()
    import library_identity as li
    import queue_store as qs
    import shows_store
    root = tmp_path / "media"
    show_dir = root / "Test Show"
    (show_dir / "Season 01").mkdir(parents=True)
    ep = show_dir / "Season 01" / "Test Show S01E01.mkv"
    ep.write_bytes(b"x" * 64)
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="tv")])
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])

    show_id = shows_store.upsert_show(title="Test Show", media_type="tv",
                                      source="tvdb", external_id="55", year=2000)
    shows_store.add_show_folder(show_id, str(show_dir))
    req = qs.add_request("Test Show", "t", media_type="tv",
                         resolved_title="Test Show", external_id="55",
                         identity_source="tvdb", status=qs.STATUS_OPEN)

    summary = library_watch.process_settled_paths([str(show_dir)])

    assert summary["index"]["added"] >= 1
    idrow = li.get_identity(str(ep))
    assert idrow is not None and idrow.external_id == "55"
    assert qs.get_request(req.request_id).status == qs.STATUS_FULFILLED
    assert summary["reconcile"]["closed"] >= 1


def test_process_settled_paths_stops_on_guard_refusal(tmp_path, monkeypatch):
    """When the scoped delta is refused by the collapse guard, the pipeline
    stops after step 1 and never forces past it."""
    _clear_shared()
    root = tmp_path / "media"
    sub = root / "show"
    sub.mkdir(parents=True)                # exists, empty on disk
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])
    _index([(str(sub / f"ep{i:04d}.mkv"), f"ep{i:04d}.mkv")
            for i in range(150)])

    summary = library_watch.process_settled_paths([str(sub)])

    assert summary["index"]["aborted"]
    assert "reconcile" not in summary      # stopped before reconciliation
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM library_files").fetchone()[0] == 150


def test_process_settled_paths_empty_scope_is_noop():
    assert library_watch.process_settled_paths([]) == {"scope": 0}


# ---------------------------------------------------------------------------
# 5. Real watchdog observer on a tmp dir + graceful-off paths.
# ---------------------------------------------------------------------------

def test_watcher_fires_on_real_file(tmp_path):
    pytest.importorskip("watchdog")
    root = tmp_path / "media"
    (root / "show").mkdir(parents=True)
    got: list[str] = []
    lock = threading.Lock()

    def on_batch(paths):
        with lock:
            got.extend(paths)

    watcher = library_watch.LibraryWatcher(
        on_batch, roots=[str(root)], settle_seconds=0.4, poll_interval=0.1)
    assert watcher.start() is True
    try:
        target = root / "show" / "new.mkv"
        target.write_bytes(b"x" * 32)
        deadline = time.time() + 15
        while time.time() < deadline:
            with lock:
                if got:
                    break
            time.sleep(0.1)
    finally:
        watcher.stop()

    with lock:
        seen = [os.path.normcase(p) for p in got]
    assert any(os.path.normcase(str(target)) == s for s in seen), seen


def test_watcher_unwatchable_root_is_off(tmp_path):
    pytest.importorskip("watchdog")
    watcher = library_watch.LibraryWatcher(
        lambda p: None, roots=[str(tmp_path / "does_not_exist")])
    assert watcher.start() is False        # no watchable root -> feature off
    watcher.stop()                         # safe even though never started


def test_watcher_no_roots_is_off_and_never_raises():
    # With OR without watchdog: no configured roots -> start() returns False
    # (feature off), never an exception.
    watcher = library_watch.LibraryWatcher(lambda p: None, roots=[])
    assert watcher.start() is False


def test_watchdog_missing_notice_logs_once(monkeypatch):
    """The 'watchdog not installed' notice is emitted at most once per process."""
    monkeypatch.setattr(library_watch, "_missing_logged", False)
    monkeypatch.setattr(library_watch, "_OBSERVER_CLS", None)
    calls = {"n": 0}
    real = library_watch.logger.info
    monkeypatch.setattr(library_watch.logger, "info",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1),
                                         real(*a, **k))[1])
    assert library_watch.watchdog_available() is False
    library_watch._log_watchdog_missing()
    library_watch._log_watchdog_missing()
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 6. Idle-pass additions + watcher lifecycle — source-level wiring, no GUI.
#    (Same technique as tests/test_maint_wiring.py: importing the class does
#    not touch Tk; instantiating it would, so we inspect source instead. The
#    source is read fresh from disk each time — like this repo's existing
#    test_no_stale_watchlist_recs_label_remains — rather than via inspect's
#    linecache, so it stays correct even while other work edits these files.)
# ---------------------------------------------------------------------------

def _method_src(module, def_signature: str) -> str:
    import pathlib
    text = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    start = text.index(def_signature)
    nxt = text.find("\n    def ", start + 1)   # next method at class indent
    return text[start:nxt if nxt != -1 else len(text)]


def test_idle_pass_adds_identity_recs_and_show_sync():
    import desktop_app
    src = _method_src(desktop_app, "    def _run_idle_cache_pass(self)")
    assert "_run_identity_backfill(from_idle=True)" in src, (
        "idle pass must warm the library identity backfill (Task K item 4)")
    assert "refresh_recs_headless()" in src, (
        "idle pass must refresh recommendations (Task K item 4)")
    assert "show_tracker.sync_all(" in src, (
        "idle pass must sync tracked-show episodes (Task K item 4)")
    # Registered through the same job registry as the other pre-cache jobs.
    assert '"idle_recs_refresh"' in src and '"idle_shows_sync"' in src


def test_recs_refresh_headless_reuses_the_recommender():
    import watchlist_tab
    assert callable(watchlist_tab.WatchlistTab.refresh_recs_headless)
    src = _method_src(watchlist_tab, "    def refresh_recs_headless(self)")
    assert "get_recommendations" in src   # no second recommender
    assert "_persist_recs()" in src       # writes the cache the tab reads


def test_watcher_started_after_ui_init_and_stopped_on_shutdown():
    import desktop_app
    init_src = _method_src(desktop_app, "    def _initialize_runtime_state(self)")
    assert "self._start_library_watcher()" in init_src

    start_src = _method_src(desktop_app, "    def _start_library_watcher(self)")
    assert "LIBRARY_WATCH_ENABLED" in start_src   # honors the config toggle
    assert "library_watch.LibraryWatcher" in start_src

    shutdown_src = _method_src(desktop_app, "    def _shutdown(self")
    assert "self._library_watcher.stop()" in shutdown_src


def test_watch_batch_runs_through_the_job_registry():
    import desktop_app
    src = _method_src(desktop_app, "    def _on_watch_batch(self")
    assert "process_settled_paths" in src
    assert "_maint_submit(" in src        # visible, serialized, journalled
    assert "_post_to_ui(" in src          # poller thread must not touch Tk
