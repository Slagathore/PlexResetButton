// =============================================================================
// download.mjs — headless webtorrent downloader for Sensarr
// =============================================================================
// Usage: node download.mjs <magnet-uri> <destination-dir> [stallTimeoutSec] [selectionJson]
//
// Protocol: newline-delimited JSON on stdout, consumed by download_manager.py:
//   {"event":"metadata","name":...,"files":[{"path":...,"size":...}]}
//   {"event":"progress","progress":0.42,"downloadSpeed":123456,"peers":7}
//   {"event":"done","name":...,"files":[{"path":...,"size":...}]}
//   {"event":"error","message":"..."}
//
// Seeding stops the moment the download completes (client.destroy on "done") —
// this runner never uploads beyond what the swarm gets during the download.
//
// Credit: the design of this pipeline (webtorrent engine, per-category
// sources, stop-seed-on-complete) is modeled on torlink by bairon
// (https://github.com/baairon/torlink, MIT). See README Acknowledgements.
// =============================================================================

import WebTorrent from "webtorrent";

const [magnet, destDir, stallTimeoutArg, selectionArg] = process.argv.slice(2);

// Deterministic no-network smoke mode (Task H item 9): prove the runner's
// dependency tree loads and a client constructs/destroys cleanly, with every
// peer-discovery mechanism disabled so nothing touches the network. CI runs
// this; process.exit here means the download flow below never starts.
if (magnet === "--smoke-test") {
  const smokeClient = new WebTorrent({
    dht: false, tracker: false, lsd: false, utPex: false, webSeeds: false,
  });
  console.log(JSON.stringify({ event: "smoke", ok: true, webtorrent: WebTorrent.VERSION || "unknown" }));
  const destroyed = new Promise((resolve) => smokeClient.destroy(resolve));
  const timedOut = new Promise((resolve) => setTimeout(() => resolve("timeout"), 10_000).unref());
  const result = await Promise.race([destroyed, timedOut]);
  if (result === "timeout") {
    console.log(JSON.stringify({ event: "error", message: "smoke destroy timed out" }));
    process.exit(1);
  }
  process.exit(0);
}

if (!magnet || !destDir) {
  console.log(JSON.stringify({ event: "error", message: "usage: download.mjs <magnet> <destDir> [stallSec]" }));
  process.exit(2);
}
const STALL_MS = (parseInt(stallTimeoutArg, 10) || 900) * 1000;
let selection = {};
try {
  selection = selectionArg ? JSON.parse(selectionArg) : {};
} catch {
  selection = {};
}

const emit = (obj) => console.log(JSON.stringify(obj));

const client = new WebTorrent();
let finished = false;

const die = (code) => {
  finished = true;
  client.destroy(() => process.exit(code));
  // Belt and braces: force-exit if destroy hangs.
  setTimeout(() => process.exit(code), 10_000).unref();
};

client.on("error", (err) => {
  emit({ event: "error", message: String(err.message || err) });
  die(1);
});

let lastDownloaded = 0;
let lastActivity = Date.now();
const startedAt = Date.now();
let wantedFiles = [];

const torrent = client.add(magnet, { path: destDir });

torrent.on("error", (err) => {
  emit({ event: "error", message: String(err.message || err) });
  die(1);
});

const extension = (filePath) => {
  const match = String(filePath || "").toLowerCase().match(/(\.[^.\\/]+)$/);
  return match ? match[1] : "";
};

const pathTokens = (filePath) => new Set(
  String(filePath || "").toLowerCase()
    .split(/[\\/\s._\-\[\](){}]+/)
    .filter(Boolean)
);

const episodeMarker = (filePath) => {
  const text = String(filePath || "");
  let match = text.match(/(?:^|\D)s(\d{1,2})[ ._\-]*e(\d{1,3})(?:\D|$)/i);
  if (match) return { season: Number(match[1]), episode: Number(match[2]) };
  match = text.match(/(?:^|\D)(\d{1,2})x(\d{1,3})(?:\D|$)/i);
  return match ? { season: Number(match[1]), episode: Number(match[2]) } : null;
};

const subtitleLanguageOk = (filePath) => {
  const spec = selection.subtitleLanguage || {};
  const tokens = pathTokens(filePath);
  const all = new Set(spec.allLanguageTokens || []);
  const wanted = new Set(spec.wantedTokens || []);
  const found = [...tokens].filter((token) => all.has(token));
  return found.length === 0 || found.some((token) => wanted.has(token));
};

const chooseFiles = () => {
  const videoExts = new Set(selection.videoExtensions || []);
  const subtitleExts = new Set(selection.subtitleExtensions || []);
  const videos = torrent.files.filter((file) => videoExts.has(extension(file.path)));
  const wantedSeason = Number.isInteger(selection.season) ? selection.season : null;
  const wantedEpisode = Number.isInteger(selection.episode) ? selection.episode : null;

  let selectedVideos = videos;
  if (wantedEpisode !== null) {
    const exact = videos.filter((file) => {
      const marker = episodeMarker(file.path);
      return marker && marker.episode === wantedEpisode
        && (wantedSeason === null || marker.season === wantedSeason);
    });
    // Never guess: prune a pack only after positively finding the target.
    if (exact.length > 0) selectedVideos = exact;
  } else if (wantedSeason !== null) {
    const exactSeason = videos.filter((file) => {
      const marker = episodeMarker(file.path);
      return marker && marker.season === wantedSeason;
    });
    if (exactSeason.length > 0) selectedVideos = exactSeason;
  }

  const selected = new Set(selectedVideos);
  for (const file of torrent.files) {
    if (!subtitleExts.has(extension(file.path)) || !subtitleLanguageOk(file.path)) continue;
    const marker = episodeMarker(file.path);
    if (wantedEpisode !== null && marker
        && (marker.episode !== wantedEpisode
            || (wantedSeason !== null && marker.season !== wantedSeason))) continue;
    if (wantedEpisode === null && wantedSeason !== null && marker
        && marker.season !== wantedSeason) continue;
    selected.add(file);
  }

  torrent.files.forEach((file) => file.deselect());
  selected.forEach((file) => file.select());
  return [...selected];
};

torrent.on("ready", () => {
  lastActivity = Date.now();
  const selectedFiles = chooseFiles();
  wantedFiles = selectedFiles;
  const videoExts = new Set(selection.videoExtensions || []);
  if (!selectedFiles.some((file) => videoExts.has(extension(file.path)))) {
    emit({ event: "error", message: "torrent metadata contains no selectable video file" });
    die(1);
    return;
  }
  emit({
    event: "metadata",
    name: torrent.name,
    files: selectedFiles.map((f) => ({ path: f.path, size: f.length })),
    skippedFiles: torrent.files.length - selectedFiles.length,
  });
});

const progressTimer = setInterval(() => {
  if (finished) return;
  const maxRuntimeMs = Number(selection.maxRuntimeSeconds || 0) * 1000;
  if (maxRuntimeMs > 0 && Date.now() - startedAt > maxRuntimeMs) {
    emit({
      event: "error",
      message: `timed out: exceeded maximum runtime of ${selection.maxRuntimeSeconds}s`,
    });
    clearInterval(progressTimer);
    die(1);
    return;
  }
  if (torrent.downloaded > lastDownloaded) {
    lastDownloaded = torrent.downloaded;
    lastActivity = Date.now();
  } else if (Date.now() - lastActivity > STALL_MS) {
    emit({ event: "error", message: `stalled: no data for ${STALL_MS / 1000}s` });
    clearInterval(progressTimer);
    die(1);
    return;
  }
  emit({
    event: "progress",
    progress: Number(torrent.progress.toFixed(4)),
    downloadSpeed: Math.round(torrent.downloadSpeed),
    peers: torrent.numPeers,
  });
}, 2000);

torrent.on("done", () => {
  clearInterval(progressTimer);
  emit({
    event: "done",
    name: torrent.name,
    files: wantedFiles.map((f) => ({ path: f.path, size: f.length })),
  });
  die(0); // destroy immediately — stops seeding
});
