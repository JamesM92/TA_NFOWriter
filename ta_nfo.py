#!/usr/bin/env python3
"""Write Jellyfin-compatible .nfo files and artwork for a TubeArchivist library, using only the
metadata embedded in the MP4 files. No TubeArchivist server, no Jellyfin plugin, and no third-party
Python packages are needed.

Layout expected: <library>/<UC channel id>/<video id>.mp4

Per video:   <id>.nfo, <id>-thumb.jpg      (from the covr atom)
Per channel: tvshow.nfo, poster.jpg, banner.jpg, fanart.jpg   (from the channel_* atoms)

Metadata comes from TubeArchivist's embedded "ta" JSON tag when present and falls back to the
standard MP4 atoms. Channel data is taken from the most recently modified video.
Season = upload year; shorts use year + 10000 and live streams year + 20000.
Episode = MMDDHH of the upload time (UTC), so it sorts chronologically; only uploads in the same hour share a number.

By default (view mode) the tool builds a separate browsable library in the folder that holds this script
(or --view-dir DIR) with readable names and symlinks (or hard links) to the videos. The TubeArchivist
folder is never modified. --in-place writes the NFOs and artwork beside the videos instead. --playlists also writes one .m3u8 per
TubeArchivist playlist into <view>/Playlists. See the README. Symlink targets are absolute host paths, so mount the library and the
view at the same paths in every container (or use --link-type hard).

Safe to run daily from cron:
  * A video is only read when its .nfo is missing or older than the .mp4 (one stat() per unchanged file).
  * Videos modified in the last --min-age minutes are skipped (still downloading or being re-embedded).
  * Channel info (tvshow.nfo + channel images) is rebuilt only when a video was added or changed since it
    was written AND it is at least --channel-refresh-days old (default 183, about six months). It is also
    rebuilt early when the existing channel info is incomplete (no description or no poster), so data
    written before TubeArchivist embedded its tags gets fixed as soon as the files are re-embedded.
  * --cleanup removes orphaned <id>.nfo / <id>-thumb.* files whose <id>.mp4 is gone. It only touches files
    this tool writes, and skips any channel folder that has no videos at all.
  * New files copy the mode and owner of the video (disable with --no-match-owner).
  * Exit status is 1 if anything could not be read, written or refreshed, so cron/monitoring notices.

    0 4 * * *  flock -n /tmp/ta_nfo.lock python3 /path/to/view/ta_nfo.py /path/to/library --cleanup --quiet
"""
import argparse
import collections
import datetime
import json
import os
import re
import stat as statmod
import struct
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

__version__ = "0.5.1"  # bumped in the release commit; the dev branch keeps the last released value

CHANNEL_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
VIDEO_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
THUMB_RE = re.compile(r"-thumb\.(jpg|png|webp)$")
SEASON_OFFSETS = {"shorts": 10000, "streams": 20000}
DEFAULT_CHANNEL_REFRESH_DAYS = 183
PLAYLIST_FOLDER = "Playlists"  # where --playlists writes its .m3u8 files, inside the view
PLAYLIST_MARK = "# ta_nfo.py playlist_id="
TITLE_MAX_CHARS = 64  # episode title length in view file names (the NFO keeps the full title)
SCRIPT_FILE = Path(__file__).absolute()  # not resolved, so a symlinked script can be detected
SCRIPT_DIR = Path(__file__).resolve().parent  # default view location
DEFAULT_MIN_AGE_MINUTES = 10
UNREADABLE_MIN = 10  # with at least this many unreadable videos and more than UNREADABLE_SHARE of those read, it is an error
UNREADABLE_SHARE = 0.05
DEFAULT_WORKERS = 16  # parallel tag reads; each read is a handful of small network round trips on a share
PLAYLIST_SCANNED = ".scanned"  # inside <view>/Playlists: channel ids whose videos were read for playlists
CHANNEL_IMAGES = {"channel_icon": "poster", "channel_banner": "banner", "channel_tv": "fanart"}
IMAGE_EXTS = (".jpg", ".png", ".webp")
STD_ATOMS = {"©nam", "©ART", "©day", "©gen", "desc", "ldes", "covr"}
WANTED_FREEFORM = {"ta", *CHANNEL_IMAGES}
MAX_VALUE_BYTES = 256 * 1024 * 1024
WRAPPER_KEYS = ("video", "data", "ta", "metadata")
BAD_NAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
CHANNEL_UID_RE = re.compile(r'type="youtube" default="true">(UC[A-Za-z0-9_-]{22})<')
VIEW_SUFFIX = r"(\.mp4|\.nfo|-thumb\.(?:jpg|png|webp)|\.[A-Za-z0-9_-]+\.vtt)$"
# current names: 20260911_[<id>]_<Title>.mp4 ; older names (still recognised): 2026-09-11 - <Title> [<id>].mp4
VIEW_FILE_RE = re.compile(r"^(?:\d{8}|undated)_\[([A-Za-z0-9_-]{11})\]_.*?" + VIEW_SUFFIX)
OLD_VIEW_FILE_RE = re.compile(r" \[([A-Za-z0-9_-]{11})\]" + VIEW_SUFFIX)
SIDECAR_RE = re.compile(r"^([A-Za-z0-9_-]{11})(\.(?:[A-Za-z0-9_-]+\.)?vtt)$")
SEASON_LABELS = {"shorts": " Shorts", "streams": " Streams"}


# ---------------------------------------------------------------- MP4 reading (standard library only)

def _boxes(f, start, end):
    """Yield (type, body_start, box_end) for the boxes in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return
        size, typ = struct.unpack(">I4s", header)
        hlen = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen or pos + size > end:
            return
        yield typ, pos + hlen, pos + size
        pos += size


def _find(f, start, end, typ):
    for t, body, stop in _boxes(f, start, end):
        if t == typ:
            return body, stop
    return None


def _data_payload(f, start, end):
    """Value of the 'data' child of an ilst item (8 bytes of type/locale precede it)."""
    box = _find(f, start, end, b"data")
    if not box or box[1] - box[0] < 8 or box[1] - box[0] - 8 > MAX_VALUE_BYTES:
        return None
    f.seek(box[0] + 8)
    return f.read(box[1] - box[0] - 8)


def _read_meta(f, start, end, tags, skip=()):
    ilst = _find(f, start, end, b"ilst")
    if not ilst:
        return
    # QuickTime "mdta" layout: a keys box names the entries and ilst items are numbered 1..n.
    keys = {}
    kbox = _find(f, start, end, b"keys")
    if kbox:
        f.seek(kbox[0] + 4)
        (count,) = struct.unpack(">I", f.read(4))
        pos = kbox[0] + 8
        for i in range(1, count + 1):
            f.seek(pos)
            head = f.read(8)
            if len(head) < 8:
                break
            size = struct.unpack(">I", head[:4])[0]
            if size < 8 or pos + size > kbox[1]:
                break
            keys[i] = f.read(size - 8).decode("utf-8", "replace")
            pos += size
    for typ, body, stop in _boxes(f, ilst[0], ilst[1]):
        if typ == b"----":
            name_box = _find(f, body, stop, b"name")
            if not name_box:
                continue
            f.seek(name_box[0] + 4)
            name = f.read(name_box[1] - name_box[0] - 4).decode("utf-8", "replace")
            if name in WANTED_FREEFORM and name not in skip and name not in tags:
                value = _data_payload(f, body, stop)
                if value is not None:
                    tags[name] = value
            continue
        name = typ.decode("latin-1")
        if name in STD_ATOMS:
            if name in skip:
                continue
            key = name
        elif len(typ) == 4 and struct.unpack(">I", typ)[0] in keys:
            key = keys[struct.unpack(">I", typ)[0]]
            if key not in WANTED_FREEFORM or key in skip:
                continue
        else:
            continue
        if key not in tags:
            value = _data_payload(f, body, stop)
            if value is not None:
                tags[key] = value


def read_tags(path, skip=()):
    """Return {atom/tag name: raw bytes} for the atoms this tool uses, without the ones named in `skip`."""
    tags = {}
    with open(path, "rb") as f:
        try:  # we jump around the file, so do not let the kernel (or an SMB client) read ahead megabytes
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_RANDOM)
        except (AttributeError, OSError):
            pass
        st = os.fstat(f.fileno())
        size = st.st_size
        moov = _find(f, 0, size, b"moov")
        if not moov:
            age = max(time.time() - st.st_mtime, 0)
            what = ("the file is empty (0 bytes)" if size == 0
                    else f"{size:,} bytes, last modified {fmt_duration(age)} ago")
            raise ValueError(f"not a readable MP4 (no moov box; {what}; truncated, or still being written?)")
        udta = _find(f, moov[0], moov[1], b"udta")
        for parent in (udta, moov):
            if not parent:
                continue
            meta = _find(f, parent[0], parent[1], b"meta")
            if meta:
                _read_meta(f, meta[0] + 4, meta[1], tags, skip)  # "meta" is a full box: skip version/flags
    return tags


def image_ext(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _find_video_obj(doc):
    """The TA index document, at the root or under a wrapper object."""
    if not isinstance(doc, dict):
        return {}
    for candidate in (doc, *(doc.get(k) for k in WRAPPER_KEYS)):
        if isinstance(candidate, dict) and ("youtube_id" in candidate or "title" in candidate or "channel" in candidate):
            return candidate
    return {}


def _playlists(raw):
    """[{id, name, refresh, entries: [{id, title, idx}]}] from the ta "playlists" list (ignores malformed items)."""
    out = []
    for p in raw if isinstance(raw, list) else []:
        if not (isinstance(p, dict) and isinstance(p.get("playlist_id"), str) and p["playlist_id"]):
            continue
        entries = [{"id": e["youtube_id"], "title": e.get("title"), "idx": e.get("idx")}
                   for e in p.get("playlist_entries") or []
                   if isinstance(e, dict) and isinstance(e.get("youtube_id"), str)]
        refresh = p.get("playlist_last_refresh")
        out.append({"id": p["playlist_id"], "name": p.get("playlist_name") or p["playlist_id"],
                    "refresh": refresh if isinstance(refresh, (int, float)) and not isinstance(refresh, bool) else 0,
                    "entries": entries})
    return out


def read(mp4, images=False, thumb=True):
    """Metadata for one video: the 'ta' tag wins, the standard atoms fill the gaps.

    The three channel images (about 0.4 MB per video, identical across a channel) are only read with images=True,
    and the episode thumbnail (60 to 90 KB) only with thumb=True."""
    skip = () if images else tuple(CHANNEL_IMAGES)
    tags = read_tags(mp4, skip=skip if thumb else (*skip, "covr"))

    def text(key):
        raw = tags.get(key)
        value = raw.decode("utf-8", "replace").strip() if raw else ""
        return value or None

    ta_doc = None
    if tags.get("ta"):
        try:
            ta_doc = json.loads(tags["ta"])
        except ValueError:
            pass
    video = _find_video_obj(ta_doc)
    channel = video.get("channel") if isinstance(video.get("channel"), dict) else {}

    date = moment = None
    published = video.get("published")
    if isinstance(published, (int, float)) and not isinstance(published, bool):
        moment = datetime.datetime.fromtimestamp(published, datetime.timezone.utc)
        date = moment.date()
    elif isinstance(published, str):
        try:
            date = datetime.datetime.fromisoformat(published[:10]).date()
        except ValueError:
            pass
    if date is None and text("©day"):
        try:
            date = datetime.datetime.strptime(text("©day")[:8], "%Y%m%d").date()
        except ValueError:
            pass

    downloaded = video.get("date_downloaded")
    player = video.get("player") if isinstance(video.get("player"), dict) else {}
    return {
        "title": video.get("title") or text("©nam"),
        "plot": video.get("description") or text("desc") or text("ldes"),
        "artist": channel.get("channel_name") or text("©ART"),
        "date": date,
        "moment": moment,  # upload time (UTC) when TubeArchivist gave a full timestamp, else None
        "genres": video.get("category") or ([text("©gen")] if text("©gen") else []),
        "tags": video.get("tags") or [],
        "vid_type": video.get("vid_type"),
        "duration": player.get("duration"),
        "added": (datetime.datetime.fromtimestamp(downloaded, datetime.timezone.utc)
                  if isinstance(downloaded, (int, float)) and not isinstance(downloaded, bool) else None),
        "chan_desc": channel.get("channel_description"),
        "chan_tags": channel.get("channel_tags") or [],
        "thumb": tags.get("covr"),
        "playlists": _playlists((ta_doc.get("playlists") if isinstance(ta_doc, dict) else None) or video.get("playlists")),
        "chan_images": {k: tags.get(k) for k in CHANNEL_IMAGES},
    }


# ---------------------------------------------------------------- NFO building

def season_number(date, vid_type):
    return date.year + SEASON_OFFSETS.get(vid_type or "", 0)


def sub(parent, name, text, **attr):
    if text not in (None, ""):
        ET.SubElement(parent, name, attr).text = XML_BAD.sub("", str(text))


def episode_number(date, moment=None):
    """MMDDHH of the upload time (UTC) as an integer, e.g. 2026-09-11 14:30:11 -> 91114.

    It sorts chronologically within a season (the upload year) and, unlike a per-day counter, needs no knowledge of the
    other videos, so it is stable across incremental runs. Minutes are left out on purpose: two uploads in the same hour
    share a number, which is accepted. A video that only has a date (no time of day) gets MMDD00."""
    hour = moment.hour if moment else 0
    return (date.month * 100 + date.day) * 100 + hour


def episode_nfo(meta, video_id):
    root = ET.Element("episodedetails")
    sub(root, "title", meta["title"])
    sub(root, "showtitle", meta["artist"])
    sub(root, "plot", meta["plot"])
    d = meta["date"]
    if d:
        sub(root, "season", season_number(d, meta["vid_type"]))
        sub(root, "episode", episode_number(d, meta["moment"]))
        sub(root, "aired", d.isoformat())
        sub(root, "premiered", d.isoformat())
        sub(root, "year", d.year)
    if isinstance(meta["duration"], (int, float)) and meta["duration"] > 0:
        sub(root, "runtime", max(1, round(meta["duration"] / 60)))
    for g in meta["genres"]:
        sub(root, "genre", g)
    for t in meta["tags"]:
        sub(root, "tag", t)
    sub(root, "studio", meta["artist"])
    if meta["added"]:
        sub(root, "dateadded", meta["added"].strftime("%Y-%m-%d %H:%M:%S"))
    sub(root, "uniqueid", video_id, type="youtube", default="true")
    return root


def tvshow_nfo(meta, channel_id):
    root = ET.Element("tvshow")
    name = meta["artist"] or channel_id
    sub(root, "title", name)
    sub(root, "showtitle", name)
    sub(root, "plot", meta["chan_desc"])
    for g in meta["genres"]:
        sub(root, "genre", g)
    for t in meta["chan_tags"]:
        sub(root, "tag", t)
    if meta["artist"]:
        sub(root, "studio", meta["artist"])
    sub(root, "uniqueid", channel_id, type="youtube", default="true")
    return root


# ---------------------------------------------------------------- reporting

class Reporter:
    def __init__(self, quiet, logfile):
        self.quiet = quiet
        self.logfile = logfile
        self.errors = 0
        self._seen = set()

    def _log(self, msg):
        if self.logfile:
            with open(self.logfile, "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")

    def info(self, msg):
        if not self.quiet:
            print(msg)
        self._log(msg)

    def summary(self, msg):
        print(msg)
        self._log(msg)

    def progress(self, msg):  # --progress lines: shown even with --quiet
        print(msg, flush=True)
        self._log(msg)

    def warning(self, msg):  # something to read, but not a reason to fail the run
        if msg in self._seen:
            return
        self._seen.add(msg)
        print(f"warning: {msg}", file=sys.stderr)
        self._log(f"warning: {msg}")

    def error(self, msg):
        if msg in self._seen:  # the same problem seen twice in one run (a video read for two reasons) counts once
            return
        self._seen.add(msg)
        self.errors += 1
        print(f"error: {msg}", file=sys.stderr)
        self._log(f"error: {msg}")


# ---------------------------------------------------------------- file handling

def stale(out: Path, src_mtime: float) -> bool:
    """True when `out` is missing or older than the source (make-style freshness check)."""
    try:
        return out.stat().st_mtime < src_mtime
    except FileNotFoundError:
        return True


def atomic_write(path: Path, data: bytes, ref: Path, match_owner: bool):
    # Write beside the target and rename, so Jellyfin never sees a half-written file.
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        if match_owner:
            st = ref.stat()
            os.chmod(tmp, statmod.S_IMODE(st.st_mode) & 0o666)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass  # only root (or a group member) may change ownership; keep the cron user's
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


class Ctx:
    def __init__(self, args, rep):
        self.args = args
        self.rep = rep
        self.stats = {"videos": 0, "written": 0, "removed": 0, "deferred": 0}
        self.playlists = {}  # playlist id -> newest playlist record seen this run (with "ref": a video path)
        self.links = {}  # video id -> episode link path made or planned this run
        self.track_scan = False  # --playlists: read each channel's videos once, remembering the channels done
        self.scanned = set()  # channel ids already read for playlists (kept in <view>/Playlists/.scanned)
        self.dirty = set()  # playlist ids collected or changed since their file was last written
        self.links_preloaded = False  # ctx.links already holds every episode link in the view (--playlists)
        self.unreadable = {}  # video path -> why it is not a usable MP4; warnings, not errors (see read_failed)
        self.window = 4 * DEFAULT_WORKERS  # reads allowed to run ahead of the writer
        self.timing = {"scan": 0.0, "wait": 0.0}
        self.stats["read"] = 0  # videos whose tags were read
        self.stats["videos_done"] = 0  # videos handled so far (read or skipped), for --progress
        self.total_videos = 0  # from a cheap pre-count, only with --progress
        self.started = time.monotonic()
        self.last_tick = self.started
        self.recent = collections.deque(maxlen=12)  # (time, videos handled) samples for the rate

    def write(self, path: Path, data: bytes, ref: Path):
        self.rep.info(f"write {path}")
        if self.args.dry_run:
            self.stats["written"] += 1
            return
        try:
            atomic_write(path, data, ref, not self.args.no_match_owner)
            self.stats["written"] += 1
        except OSError as e:
            self.rep.error(f"cannot write {path}: {e}")

    def read_failed(self, path, err):
        """A video could not be read. If the file was read but is not a usable MP4 (cut off, empty, bad structure) that is
        a warning: the video is skipped and tried again next run, and one broken file must not turn every run red. If it
        could not be read at all (permission denied, I/O error, the share gone) it is an error."""
        if isinstance(err, (ValueError, struct.error)):
            if path not in self.unreadable:
                self.unreadable[path] = str(err)
                self.rep.warning(f"cannot read {path}: {err}")
        else:
            self.rep.error(f"cannot read {path}: {err}")

    def collect_playlists(self, meta, ref: Path):
        for pl in meta["playlists"]:
            best = self.playlists.get(pl["id"])
            if best is None or pl["refresh"] >= best["refresh"]:
                self.playlists[pl["id"]] = {**pl, "ref": ref}
                self.dirty.add(pl["id"])

    def tick(self, name, done, total):
        """--progress: a heartbeat line at most every 30 s, so a big channel does not look stuck."""
        if not self.args.progress:
            return
        now = time.monotonic()
        if now - self.last_tick >= 30:
            self.last_tick = now
            self.rep.progress(f"  ... {name}: {done:,}/{total:,} videos, {self.rate_text(now)}")

    def rate_text(self, now):
        handled = self.stats["videos_done"]
        self.recent.append((now, handled))
        elapsed = now - self.started
        t0, h0 = self.recent[0]
        per_s = (handled - h0) / (now - t0) if now - t0 >= 5 else handled / max(elapsed, 1e-9)
        text = f"{per_s:,.1f} videos/s, elapsed {fmt_duration(elapsed)}"
        if self.total_videos and per_s > 0:
            left = max(self.total_videos - handled, 0) / per_s
            text += f", about {fmt_duration(left)} left ({100 * handled / self.total_videos:.0f}% done)"
        return text

    def write_xml(self, path: Path, root, ref: Path):
        ET.indent(root)
        body = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
        self.write(path, body.encode("utf-8"), ref)

    def write_image(self, folder: Path, stem: str, data, ref: Path):
        if data:
            self.write(folder / f"{stem}{image_ext(data)}", data, ref)


def fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def scan_videos(folder: Path, pool=None):
    """[(path, mtime)] for recognisable videos, newest first (ties broken by name).

    Each stat is a network round trip on a share, so with a pool they run in parallel."""
    entries = []
    with os.scandir(folder) as it:
        for e in it:
            if e.name.endswith(".mp4") and VIDEO_RE.match(e.name[:-4]) and e.is_file():
                entries.append(e)

    def mtime(e):
        try:
            return e.stat().st_mtime
        except OSError:  # vanished since the listing
            return None

    mtimes = pool.map(mtime, entries) if pool is not None and len(entries) > 32 else map(mtime, entries)
    found = [(Path(e.path), m) for e, m in zip(entries, mtimes) if m is not None]
    found.sort(key=lambda v: (-v[1], v[0].name))
    return found


def prefetch(pool, func, items, window):
    """Yield func(item) for each item in order, keeping up to `window` calls running ahead of the consumer.

    Unlike pool.map this never queues everything at once (memory stays flat) and the reads overlap the
    consumer's writes, which is what hides the network delay."""
    pending = collections.deque()
    source = iter(items)

    def submit():
        for item in source:
            pending.append(pool.submit(func, item))
            return

    for _ in range(window):  # reading starts now, not when the first result is asked for
        submit()

    def results():
        try:
            while pending:
                result = pending.popleft().result()
                submit()
                yield result
        finally:
            for fut in pending:
                fut.cancel()

    return results()


def timed(ctx, key, results):
    """Pass `results` through, adding the time spent waiting for each item to ctx.timing[key]."""
    it = iter(results)
    while True:
        t = time.perf_counter()
        try:
            item = next(it)
        except StopIteration:
            return
        ctx.timing[key] += time.perf_counter() - t
        yield item


RETRY_DELAY = 0.5  # seconds before the one retry of a read that failed


def safe_read(path: Path, images=False, thumb=True):
    """(path, metadata, None) or (path, None, error). A failed read is tried once more after a short pause: a busy
    network share can return a short read or an error once, and that must not look like a corrupt video."""
    for attempt in (1, 2):
        try:
            return path, read(path, images, thumb), None
        except (ValueError, OSError, struct.error) as e:  # unreadable, truncated or a hiccup on the share
            if attempt == 2:
                return path, None, e
            time.sleep(RETRY_DELAY)
        except Exception as e:  # anything else is a bug or a corrupt file; do not retry
            return path, None, e


def newest_readable(videos, first, images, ctx, tries=5):
    """(path, metadata) of the newest video that can be read, or (None, None).

    `first` is the (path, metadata, error) already read for videos[0]. The series info comes from the newest video,
    so when that one is unreadable (a half-downloaded upload, say) the next few are tried instead of giving up on
    the whole channel."""
    path, meta, _ = first
    if meta is not None:
        return path, meta
    for p, _ in videos[1:tries]:
        _, m, err = safe_read(p, images)
        if m is not None:
            return p, m
        ctx.read_failed(p, err)
    return None, None


def find_channel_images(videos, ctx, first=None):
    """{atom name: image bytes}: the newest video's channel artwork, with older videos opened only for what it lacks.

    `videos` is newest first. Only called when the channel info is being written. `first` is the newest video's
    metadata when it was already read with its images. Videos are read one at a time and not kept, so memory
    stays flat however many videos a channel has."""
    found = {}
    for number, (p, _) in enumerate(videos):
        if len(found) == len(CHANNEL_IMAGES):
            break
        if number == 0 and first is not None:
            m, err = first, None
        else:
            _, m, err = safe_read(p, images=True)
        if err:
            ctx.read_failed(p, err)
        for atom in CHANNEL_IMAGES:
            if atom not in found and m and m["chan_images"][atom]:
                found[atom] = m["chan_images"][atom]
    return found


def channel_incomplete(tvshow: Path, folder: Path) -> bool:
    """Existing channel info lacks a description or a poster (typically written before the tags were embedded)."""
    try:
        text = tvshow.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return "<plot>" not in text or not any((folder / f"poster{ext}").exists() for ext in IMAGE_EXTS)


def channel_due(tvshow: Path, folder: Path, newest_mtime: float, added: bool, refresh_days: float) -> bool:
    try:
        written = tvshow.stat().st_mtime
    except FileNotFoundError:
        return True
    if not (added or newest_mtime > written):
        return False
    age_days = (time.time() - written) / 86400
    return age_days >= refresh_days or channel_incomplete(tvshow, folder)


def cleanup_channel(folder: Path, videos, ctx: Ctx):
    """Remove <id>.nfo / <id>-thumb.* that no longer have a matching <id>.mp4."""
    if not videos:  # no videos at all: could be an unmounted/emptied share, so leave everything
        return
    ids = {p.stem for p, _ in videos}
    for f in folder.iterdir():
        m = THUMB_RE.search(f.name)
        if m:
            stem = f.name[: m.start()]
        elif f.suffix == ".nfo":
            stem = f.stem
        else:
            continue
        if not VIDEO_RE.match(stem) or stem in ids:
            continue
        if f.suffix == ".nfo":
            try:
                body = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f'type="youtube" default="true">{stem}<' not in body:
                continue  # not written by this tool
        ctx.rep.info(f"remove {f}")
        if ctx.args.dry_run:
            ctx.stats["removed"] += 1
            continue
        try:
            f.unlink()
            ctx.stats["removed"] += 1
        except OSError as e:
            ctx.rep.error(f"cannot remove {f}: {e}")


def process_channel(folder: Path, ctx: Ctx, pool):
    args = ctx.args
    t0 = time.perf_counter()
    all_videos = scan_videos(folder, pool)
    ctx.timing["scan"] += time.perf_counter() - t0
    if args.cleanup:
        cleanup_channel(folder, all_videos, ctx)
    if not all_videos:
        return
    ctx.stats["videos"] += len(all_videos)
    ctx.stats["videos_done"] += len(all_videos)

    cutoff = time.time() - args.min_age * 60
    videos = [v for v in all_videos if v[1] <= cutoff]
    ctx.stats["deferred"] += len(all_videos) - len(videos)
    if not videos:
        return

    # Episodes: only videos whose .nfo is missing or older than the .mp4 are opened (in parallel).
    todo = [(p, m) for p, m in videos if args.overwrite or stale(folder / f"{p.stem}.nfo", m)]
    added = any(not (folder / f"{p.stem}.nfo").exists() for p, _ in videos)
    reads = timed(ctx, "wait", prefetch(pool, safe_read, [p for p, _ in todo], ctx.window))
    for n, (path, meta, err) in enumerate(reads, 1):  # reads run ahead of the writes below
        ctx.stats["read"] += 1
        ctx.tick(folder.name, n, len(todo))
        if err:
            ctx.read_failed(path, err)
        if meta is None:
            continue
        ctx.write_xml(folder / f"{path.stem}.nfo", episode_nfo(meta, path.stem), path)
        ctx.write_image(folder, f"{path.stem}-thumb", meta["thumb"], path)

    # Channel info: rarely changes, so it is gated on "new video" AND "old enough" (or "incomplete").
    newest_path, newest_mtime = videos[0]
    tvshow = folder / "tvshow.nfo"
    if not (args.overwrite or channel_due(tvshow, folder, newest_mtime, added, args.channel_refresh_days)):
        return

    _, newest, err = safe_read(newest_path, images=True)
    if err:
        ctx.read_failed(newest_path, err)
    info_path, info = newest_readable(videos, (newest_path, newest, err), True, ctx)
    if info is None:
        return
    ctx.write_xml(tvshow, tvshow_nfo(info, folder.name), info_path)
    for atom_name, data in find_channel_images(videos, ctx, info if info_path == newest_path else None).items():
        ctx.write_image(folder, CHANNEL_IMAGES[atom_name], data, info_path)


# ---------------------------------------------------------------- view mode (separate, linked library)

def clean_name(text, max_bytes=120, fallback="untitled", max_chars=None):
    """A file/folder name that is safe on Linux, SMB and Windows shares."""
    name = re.sub(r"\s+", " ", BAD_NAME_CHARS.sub(" ", str(text or ""))).strip(" .")
    if max_chars and len(name) > max_chars:
        name = name[:max_chars].rstrip(" .")
    raw = name.encode("utf-8")
    if len(raw) > max_bytes:
        name = raw[:max_bytes].decode("utf-8", "ignore").rstrip(" .")
    return name or fallback


def season_folder(meta):
    """(folder name, season.nfo title) for a video; same numbers as the <season> tag."""
    d = meta["date"]
    if not d:
        return "Season 0", "Undated"
    return f"Season {season_number(d, meta['vid_type'])}", f"{d.year}{SEASON_LABELS.get(meta['vid_type'] or '', '')}"


def episode_base(meta, video_id):
    d = meta["date"]
    return f"{d.strftime('%Y%m%d') if d else 'undated'}_[{video_id}]_{clean_name(meta['title'] or video_id, max_chars=TITLE_MAX_CHARS)}"


def view_file(name):
    """(video id, suffix) for a file this tool made in a view, in the current or the older name format."""
    m = VIEW_FILE_RE.match(name) or OLD_VIEW_FILE_RE.search(name)
    return (m.group(1), m.group(2)) if m else None


def season_nfo(number, title):
    root = ET.Element("season")
    sub(root, "title", title)
    sub(root, "seasonnumber", number)
    return root


def channel_uid(tvshow: Path):
    try:
        m = CHANNEL_UID_RE.search(tvshow.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return m.group(1) if m else None


def is_empty_dir(d: Path):
    try:
        return not any(d.iterdir())
    except OSError:
        return False


class ViewIndex:
    """Channel folders already in the view, found by the channel id inside tvshow.nfo (no state file)."""

    def __init__(self, root: Path, reserved=()):
        self.root = root
        self.by_channel = {}
        self.names = {r.lower() for r in reserved}
        if root.is_dir():
            for d in root.iterdir():
                if d.is_dir():
                    cid = channel_uid(d / "tvshow.nfo")
                    if cid:
                        self.by_channel[cid] = d
                    elif is_empty_dir(d):
                        continue  # left by a run that stopped early: free to use, not a name clash
                    self.names.add(d.name.lower())

    def folder_for(self, channel_id, meta):
        """Existing folder, or a new name. A folder keeps its first name forever (Jellyfin tracks by path)."""
        if channel_id in self.by_channel:
            return self.by_channel[channel_id]
        name = clean_name(meta["artist"] or channel_id)
        if name.lower() in self.names:
            name = f"{name} [{channel_id}]"
        self.names.add(name.lower())
        self.by_channel[channel_id] = self.root / name
        return self.by_channel[channel_id]


def index_episodes(show: Path):
    """{video id: (season folder, base name)} for the links already in a view channel folder."""
    found = {}
    if not show.is_dir():
        return found
    for sd in show.iterdir():
        if not (sd.is_dir() and sd.name.startswith("Season ")):
            continue
        for f in sd.iterdir():
            m = view_file(f.name)
            if m and m[1] == ".mp4" and m[0] not in found:
                found[m[0]] = (sd, f.name[:-4])
    return found


def ensure_link(ctx: Ctx, link: Path, src: Path):
    """Create or repair `link` so it points at `src`. Returns True when something changed."""
    a = ctx.args
    if a.link_type == "hard":
        try:
            if os.path.samefile(link, src):
                return False
        except OSError:
            pass
        target = str(src)
    else:
        target = os.path.join(a.link_root, src.parent.name, src.name) if a.link_root else os.path.abspath(src)
        if link.is_symlink() and os.readlink(link) == target:
            return False
    ctx.rep.info(f"link {link} -> {target}")
    ctx.stats["written"] += 1
    if a.dry_run:
        return True
    tmp = link.with_name(link.name + ".tmp")
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        if a.link_type == "hard":
            os.link(src, tmp)
        else:
            os.symlink(target, tmp)
        tmp.replace(link)
    except OSError as e:
        ctx.stats["written"] -= 1
        ctx.rep.error(f"cannot link {link}: {e}")
        return False
    finally:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
    return True


def scan_sidecars(folder: Path):
    """{video id: [(suffix, path)]} for subtitle files such as <id>.en.vtt."""
    found = {}
    with os.scandir(folder) as it:
        for e in it:
            m = SIDECAR_RE.match(e.name)
            if m and e.is_file():
                found.setdefault(m.group(1), []).append((m.group(2), Path(e.path)))
    return found


def cleanup_view(show: Path, ids, ctx: Ctx):
    """Remove view files for videos TubeArchivist no longer has, then season folders left with nothing."""
    if not show.is_dir():
        return
    for sd in sorted(show.iterdir()):
        if not (sd.is_dir() and sd.name.startswith("Season ")):
            continue
        removed = set()
        for f in sorted(sd.iterdir()):
            m = view_file(f.name)
            if not m or m[0] in ids:
                continue
            vid, kind = m
            if kind == ".nfo":
                try:
                    body = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if f'type="youtube" default="true">{vid}<' not in body:
                    continue  # not written by this tool
            elif kind == ".mp4" or kind.endswith(".vtt"):
                if not (f.is_symlink() or (ctx.args.link_type == "hard" and f.is_file())):
                    continue  # a real file somebody put there
            ctx.rep.info(f"remove {f}")
            ctx.stats["removed"] += 1
            removed.add(f)
            if kind == ".mp4":
                ctx.links.pop(vid, None)  # no playlist should list it any more
            if not ctx.args.dry_run:
                try:
                    f.unlink()
                except OSError as e:
                    ctx.stats["removed"] -= 1
                    ctx.rep.error(f"cannot remove {f}: {e}")
        left = [f for f in sd.iterdir() if f not in removed]
        if removed and all(f.name == "season.nfo" for f in left):
            ctx.rep.info(f"remove {sd}")
            ctx.stats["removed"] += 1
            if not ctx.args.dry_run:
                try:
                    for f in left:
                        f.unlink()
                    sd.rmdir()
                except OSError as e:
                    ctx.rep.error(f"cannot remove {sd}: {e}")


def process_channel_view(folder: Path, ctx: Ctx, pool, vindex: ViewIndex):
    args = ctx.args
    t0 = time.perf_counter()
    all_videos = scan_videos(folder, pool)
    ctx.timing["scan"] += time.perf_counter() - t0
    existing = vindex.by_channel.get(folder.name)
    if args.cleanup and existing:
        if all_videos:  # no videos at all could be an unmounted share
            cleanup_view(existing, {p.stem for p, _ in all_videos}, ctx)
    if not all_videos:
        return
    ctx.stats["videos"] += len(all_videos)
    ctx.stats["videos_done"] += len(all_videos)

    cutoff = time.time() - args.min_age * 60
    videos = [v for v in all_videos if v[1] <= cutoff]
    ctx.stats["deferred"] += len(all_videos) - len(videos)
    if not videos:
        return

    episodes = index_episodes(existing) if existing else {}

    # Read only what is new or changed: no link yet, or the view .nfo is older than the TA file.
    # With --playlists a channel not yet read for playlists is read in full once (see finish_channel_playlists).
    full_scan = ctx.track_scan and folder.name not in ctx.scanned
    todo = []  # (path, mtime, needs files written?); a playlist-only re-read of a finished video skips its thumbnail
    for p, m in videos:
        needs_files = (args.overwrite or p.stem not in episodes
                       or stale(episodes[p.stem][0] / f"{episodes[p.stem][1]}.nfo", m))
        if needs_files or full_scan:
            todo.append((p, m, needs_files))
    todo_set = {p for p, _, _ in todo}

    newest_path, newest_mtime = videos[0]
    # The newest video gives the series info. A new channel also needs its artwork, so that read includes the images
    # (and doubles as the newest episode's metadata); an existing channel reads them later, only if it is due.
    images_now = existing is None or not (existing / "tvshow.nfo").exists()
    newest_future = pool.submit(safe_read, newest_path, images_now)  # first in the queue, ahead of the pipeline
    reads = timed(ctx, "wait", prefetch(pool, lambda item: safe_read(item[0], False, item[1]),
                                        [(p, files) for p, _, files in todo if p != newest_path], ctx.window))
    t0 = time.perf_counter()
    _, newest, newest_err = newest_future.result()
    ctx.timing["wait"] += time.perf_counter() - t0
    if newest_err:
        ctx.read_failed(newest_path, newest_err)
    # `newest` stays the newest video's own metadata (it is also its episode); `info` is what the series info is
    # built from, which is the same unless the newest video cannot be read.
    info_path, info = newest_readable(videos, (newest_path, newest, newest_err), images_now, ctx)
    if existing is None:
        if info is None:
            return
        show = vindex.folder_for(folder.name, info)
    else:
        show = existing
    if not args.dry_run:
        show.mkdir(parents=True, exist_ok=True)
    tvshow = show / "tvshow.nfo"
    # Write it first: it is how a later run recognises the folder, so a run that stops partway
    # (server reboot) does not leave a folder that gets a duplicate `Channel [ID]` beside it.
    fresh = not tvshow.exists()
    if fresh and info is not None:
        ctx.write_xml(tvshow, tvshow_nfo(info, folder.name), info_path)

    sidecars = scan_sidecars(folder)
    added = False
    # Videos are read ahead in parallel while the loop below writes; `todo` is in the same order as `videos`.
    for n, (path, mtime) in enumerate(videos, 1):
        vid = path.stem
        meta = None
        if path in todo_set:
            if path == newest_path:
                meta, err = newest, None  # read above, with the series info; any error was reported there
            else:
                _, meta, err = next(reads)
            ctx.stats["read"] += 1
            if err:
                ctx.read_failed(path, err)
            if meta is not None and args.playlists:
                ctx.collect_playlists(meta, path)
        ctx.tick(folder.name, n, len(videos))
        if vid in episodes:
            sdir, base = episodes[vid]
        else:
            if meta is None:
                continue
            sname, stitle = season_folder(meta)
            sdir, base = show / sname, episode_base(meta, vid)
            if not args.dry_run:
                sdir.mkdir(exist_ok=True)
            if not (sdir / "season.nfo").exists():
                number = int(sname.split()[1])
                ctx.write_xml(sdir / "season.nfo", season_nfo(number, stitle), path)
            episodes[vid] = (sdir, base)
        ctx.links[vid] = sdir / f"{base}.mp4"
        if ensure_link(ctx, sdir / f"{base}.mp4", path):
            added = added or meta is not None
        for suffix, side in sidecars.get(vid, []):
            ensure_link(ctx, sdir / f"{base}{suffix}", side)
        if meta is not None and (args.overwrite or stale(sdir / f"{base}.nfo", mtime)):
            ctx.write_xml(sdir / f"{base}.nfo", episode_nfo(meta, vid), path)
            ctx.write_image(sdir, f"{base}-thumb", meta["thumb"], path)

    if not (args.overwrite or fresh or channel_due(tvshow, show, newest_mtime, added, args.channel_refresh_days)):
        return
    if info is None:
        return
    ctx.write_xml(tvshow, tvshow_nfo(info, folder.name), info_path)
    first = info if (images_now and info_path == newest_path) else None
    for atom_name, data in find_channel_images(videos, ctx, first).items():
        ctx.write_image(show, CHANNEL_IMAGES[atom_name], data, info_path)


def scan_view_links(root: Path):
    """{video id: episode link} for every episode link already in the view."""
    found = {}
    if not root.is_dir():
        return found
    for show in root.iterdir():
        if not show.is_dir() or show.name == PLAYLIST_FOLDER:
            continue
        for sd in show.iterdir():
            if not (sd.is_dir() and sd.name.startswith("Season ")):
                continue
            for f in sd.iterdir():
                m = view_file(f.name)
                if m and m[1] == ".mp4":
                    found.setdefault(m[0], f)
    return found


def playlist_header(path: Path):
    """(playlist id, refresh time) from a playlist file this tool wrote, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            for _, line in zip(range(8), f):
                if line.startswith(PLAYLIST_MARK):
                    fields = dict(kv.split("=", 1) for kv in line[2:].split() if "=" in kv)
                    return fields["playlist_id"], float(fields.get("refreshed", 0))
    except (OSError, ValueError, KeyError):
        pass
    return None


def render_playlist(pl, entries, pdir: Path):
    lines = ["#EXTM3U", "#PLAYLIST:" + re.sub(r"\s+", " ", pl["name"]),
             f"{PLAYLIST_MARK}{pl['id']} refreshed={int(pl['refresh'])}"]
    for entry, link in entries:
        title = re.sub(r"\s+", " ", str(entry["title"] or entry["id"])).strip()
        lines += [f"#EXTINF:-1,{title}", os.path.relpath(link, pdir).replace(os.sep, "/")]
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_playlists(ctx: Ctx, root: Path, only=None):
    """One .m3u8 per playlist seen this run (or just those in `only`), listing the episode links that exist, in order."""
    todo = sorted((pid, pl) for pid, pl in ctx.playlists.items() if only is None or pid in only)
    if not todo:
        return
    pdir = root / PLAYLIST_FOLDER
    if (pdir / "tvshow.nfo").exists():
        ctx.rep.error(f"{pdir} is a channel folder; cannot write playlists there")
        return
    if not ctx.args.dry_run:
        try:
            pdir.mkdir(exist_ok=True)  # also the marker that the first full playlist scan is done
        except OSError as e:
            ctx.rep.error(f"cannot create {pdir}: {e}")
            return
    links = ctx.links if ctx.links_preloaded else {**scan_view_links(root), **ctx.links}
    known = {}
    taken = set()
    if pdir.is_dir():
        for f in pdir.glob("*.m3u8"):
            taken.add(f.name.lower())
            head = playlist_header(f)
            if head:
                known[head[0]] = (f, head[1])
    for pid, pl in todo:
        order = sorted(pl["entries"], key=lambda e: e["idx"] if isinstance(e["idx"], int) else 1 << 30)
        entries = [(e, links[e["id"]]) for e in order if e["id"] in links]
        if not entries:
            continue
        if pid in known:
            path, old = known[pid]
            if old > pl["refresh"]:
                continue  # the file came from a newer playlist snapshot than this run saw
        else:
            short = re.sub(r"^TA_playlist_", "", pid)[:8]
            name = clean_name(pl["name"], fallback=short, max_chars=TITLE_MAX_CHARS)
            if f"{name}.m3u8".lower() in taken:
                name = f"{name} [{short}]"
            path = pdir / f"{name}.m3u8"
            taken.add(path.name.lower())
        body = render_playlist(pl, entries, pdir)
        try:
            if path.read_bytes() == body:
                continue
        except OSError:
            pass
        ctx.write(path, body, pl["ref"])


def finish_channel_playlists(ctx: Ctx, root: Path, channel_id: str):
    """After a channel: write the playlists its videos touched, then remember the channel was read for playlists.

    Doing this per channel (not once at the end) is what lets an interrupted first scan resume where it stopped."""
    if ctx.dirty:
        write_playlists(ctx, root, only=set(ctx.dirty))
        ctx.dirty.clear()
    if ctx.track_scan and channel_id not in ctx.scanned:
        ctx.scanned.add(channel_id)
        if not ctx.args.dry_run:
            try:
                with open(root / PLAYLIST_FOLDER / PLAYLIST_SCANNED, "a", encoding="utf-8") as fh:
                    fh.write(channel_id + "\n")
            except OSError as e:
                ctx.rep.error(f"cannot record progress in {root / PLAYLIST_FOLDER}: {e}")


def prune_playlists(ctx: Ctx, root: Path):
    """Drop entries whose episode link is gone from playlists this tool wrote; remove a playlist left empty."""
    pdir = root / PLAYLIST_FOLDER
    if not pdir.is_dir():
        return
    for f in sorted(pdir.glob("*.m3u8")):
        if not playlist_header(f):
            continue  # not ours
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        head = [ln for ln in lines if ln.startswith("#") and not ln.startswith("#EXTINF")]
        kept, i, dropped = [], 0, False
        body = [ln for ln in lines if ln not in head]
        while i + 1 < len(body):
            info, path = body[i], body[i + 1]
            if os.path.lexists(pdir / path):
                kept += [info, path]
            else:
                dropped = True
            i += 2
        if not dropped:
            continue
        if not kept:
            ctx.rep.info(f"remove {f}")
            ctx.stats["removed"] += 1
            if not ctx.args.dry_run:
                try:
                    f.unlink()
                except OSError as e:
                    ctx.stats["removed"] -= 1
                    ctx.rep.error(f"cannot remove {f}: {e}")
            continue
        ctx.write(f, ("\n".join(head + kept) + "\n").encode("utf-8"), pdir / kept[1])


def trigger_refresh(url: str, key: str, rep: Reporter):
    req = urllib.request.Request(
        url.rstrip("/") + "/Library/Refresh", method="POST",
        headers={"Authorization": f'MediaBrowser Token="{key}"', "X-Emby-Token": key})
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
        rep.info(f"asked Jellyfin to refresh its library ({url})")
    except Exception as e:
        rep.error(f"could not trigger Jellyfin refresh: {e}")


def count_videos(folder: Path):
    """Number of .mp4 names in a folder, without a stat per file (for the --progress total)."""
    try:
        return sum(1 for name in os.listdir(folder) if name.endswith(".mp4"))
    except OSError:
        return 0


def view_problem(args):
    """Reason view mode cannot run with these arguments, or None."""
    lib, view = args.library.resolve(), args.view_dir.resolve()
    if view == lib or lib in view.parents or view in lib.parents:
        if args.view_dir_is_default:
            return (f"the script folder ({view}) must be outside the library, and must not contain it; "
                    "move the script or pass --view-dir")
        return "--view-dir must be outside the library (and must not contain it)"
    if args.link_type == "hard":
        if args.link_root:
            return "--link-root only applies to symlinks"
        probe = view
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if os.stat(probe).st_dev != os.stat(lib).st_dev:
            return "hard links need --view-dir on the same filesystem as the library; use --link-type symlink"
    return None


def link_problem(args):
    """Reason the view folder cannot hold symlinks (checked before any file is written), or None."""
    if args.link_type != "symlink" or args.dry_run:
        return None
    probe = args.view_dir / f".ta_nfo_linktest_{os.getpid()}"
    try:
        args.view_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(str(SCRIPT_FILE), probe)
    except OSError as e:
        return (f"cannot create symlinks in {args.view_dir}: {e}. View mode needs a filesystem that supports "
                "symlinks, such as a local ext4, xfs, btrfs or ZFS disk, and not an SMB/CIFS or NFS-without-symlinks "
                "mount, exFAT or FAT. Put the view on a local disk with --view-dir (the library itself can stay "
                "on a network share), or use --link-type hard (same filesystem as the library) or --in-place")
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("library", type=Path, help="TubeArchivist library root (read only is fine)")
    ap.add_argument("--overwrite", action="store_true", help="rebuild everything, ignoring timestamps")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written or removed")
    ap.add_argument("--cleanup", action="store_true",
                    help="remove .nfo/-thumb files whose .mp4 no longer exists")
    ap.add_argument("--channel-refresh-days", type=float, default=DEFAULT_CHANNEL_REFRESH_DAYS, metavar="N",
                    help="minimum age of tvshow.nfo before channel info is rebuilt (default %(default)s)")
    ap.add_argument("--min-age", type=float, default=DEFAULT_MIN_AGE_MINUTES, metavar="MINUTES",
                    help="skip videos modified within the last N minutes; 0 disables (default %(default)s)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="parallel file reads and stats; on a network share most of the time is waiting for the "
                         "network, so more helps (default %(default)s)")
    ap.add_argument("--progress", action="store_true",
                    help="print a line per channel and a heartbeat every 30 s with the rate and an estimate of "
                         "the time left, then a breakdown of where the time went; shown even with --quiet")
    ap.add_argument("--no-match-owner", action="store_true",
                    help="do not copy the video's mode and owner onto new files")
    ap.add_argument("--quiet", action="store_true", help="print only errors and the final summary")
    ap.add_argument("--log", type=Path, metavar="FILE", help="also append output to this file")
    ap.add_argument("--view-dir", type=Path, metavar="DIR",
                    help="where the readable library (links to the videos, NFOs and artwork) is built; "
                         "default: the folder that holds this script. Nothing is written under the library")
    ap.add_argument("--in-place", action="store_true",
                    help="write NFOs and artwork next to the videos, inside the library, instead of "
                         "building a view (needs a writable library)")
    ap.add_argument("--view-library", default="", metavar="NAME",
                    help="put the channels in this folder inside the view dir (default: none, the channel "
                         "folders sit directly in the view dir, next to the script)")
    ap.add_argument("--link-type", choices=("symlink", "hard"), default="symlink",
                    help="how view mode links to the videos (default %(default)s; hard needs the same filesystem)")
    ap.add_argument("--link-root", metavar="PATH",
                    help="symlink targets become PATH/<channel id>/<id>.mp4. Only for containers that cannot "
                         "mount the library at the host path (preferred: same paths everywhere, no option)")
    ap.add_argument("--playlists", action="store_true",
                    help=f"view mode: write one .m3u8 per TubeArchivist playlist into <view>/{PLAYLIST_FOLDER} "
                         "(from the playlists embedded in the videos)")
    ap.add_argument("--jellyfin-url", default=os.environ.get("JELLYFIN_URL"),
                    help="ask this Jellyfin server to rescan after changes (or set JELLYFIN_URL)")
    ap.add_argument("--jellyfin-api-key", default=os.environ.get("JELLYFIN_API_KEY"),
                    help="API key for --jellyfin-url (or set JELLYFIN_API_KEY)")
    args = ap.parse_args(argv)

    rep = Reporter(args.quiet, args.log)
    ctx = Ctx(args, rep)
    if not args.library.is_dir():
        rep.error(f"library folder not found: {args.library}")
        return 1

    if args.in_place:
        if args.view_dir or args.link_root or args.link_type != "symlink" or args.view_library or args.playlists:
            rep.error("--view-dir, --view-library, --link-type, --link-root and --playlists cannot be used with --in-place")
            return 1
    else:
        args.view_dir_is_default = args.view_dir is None
        if args.view_dir_is_default:
            if SCRIPT_FILE.is_symlink():
                rep.error(f"{SCRIPT_FILE} is a symlink, so the script folder is ambiguous; copy the script into "
                          "the folder that should hold the view, or pass --view-dir")
                return 1
            args.view_dir = SCRIPT_DIR
        problem = view_problem(args) or link_problem(args)
        if problem:
            rep.error(problem)
            return 1

    folders = sorted(p for p in args.library.iterdir() if p.is_dir() and CHANNEL_RE.match(p.name))
    if not folders:
        rep.error(f"no channel folders (UC + 22 characters) found in {args.library}; wrong path or unmounted share?")

    vindex = None if args.in_place else ViewIndex(
        args.view_dir / clean_name(args.view_library) if args.view_library else args.view_dir,
        reserved=(PLAYLIST_FOLDER,) if args.playlists else ())
    if vindex and args.playlists:
        # The first --playlists scan reads every video once. It is done channel by channel and remembered in
        # <view>/Playlists/.scanned, so an interrupted scan resumes instead of starting over. A Playlists folder
        # without that file comes from an older version that finished its scan.
        pdir = vindex.root / PLAYLIST_FOLDER
        marker = pdir / PLAYLIST_SCANNED
        try:
            if marker.exists():
                ctx.track_scan = True
                ctx.scanned = set(marker.read_text(encoding="utf-8").split())
            elif not pdir.is_dir():
                ctx.track_scan = True
                if not args.dry_run:
                    pdir.mkdir(parents=True, exist_ok=True)
                    marker.touch()
        except OSError as e:
            rep.error(f"cannot use {pdir}: {e}")
            return 1
        ctx.links.update(scan_view_links(vindex.root))  # read once; kept up to date as channels are processed
        ctx.links_preloaded = True

    workers = max(1, args.workers)
    ctx.window = max(4, 4 * workers)
    pool = ThreadPoolExecutor(max_workers=workers)
    interrupted = False
    try:
        if args.progress:
            ctx.total_videos = sum(pool.map(count_videos, folders))
            rep.progress(f"{len(folders):,} channels, {ctx.total_videos:,} videos to look at, {workers} in parallel")
        for number, folder in enumerate(folders, 1):
            started, seen, read = time.monotonic(), ctx.stats["videos_done"], ctx.stats["read"]
            try:
                if vindex:
                    process_channel_view(folder, ctx, pool, vindex)
                else:
                    process_channel(folder, ctx, pool)
            except Exception as e:  # one bad channel must not stop the others
                rep.error(f"{folder}: {e}")
            else:
                if vindex and args.playlists:
                    finish_channel_playlists(ctx, vindex.root, folder.name)
            if args.progress:
                now = time.monotonic()
                rep.progress(f"[{number}/{len(folders)}] {folder.name}: {ctx.stats['videos_done'] - seen:,} videos, "
                             f"{ctx.stats['read'] - read:,} read, {fmt_duration(now - started)}; {ctx.rate_text(now)}")
    except KeyboardInterrupt:
        interrupted = True
        rep.summary("interrupted: stopping cleanly. Files already written are complete; run the same command "
                    "again to carry on where this stopped.")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    if vindex and args.playlists:
        write_playlists(ctx, vindex.root, only=set(ctx.dirty) if interrupted else None)
        if args.cleanup and not interrupted:
            prune_playlists(ctx, vindex.root)

    s = ctx.stats
    bad = len(ctx.unreadable)
    if bad >= UNREADABLE_MIN and bad > UNREADABLE_SHARE * max(s["read"], 1):
        # A few broken files are warnings. This many means something else is wrong: the wrong folder, a sick share.
        rep.error(f"{bad:,} of the {s['read']:,} videos read could not be read; that is too many to be a few broken "
                  "files. Is the library path right and the share healthy?")
    rep.summary(f"{'stopped' if interrupted else 'done'}: {s['videos']} videos seen, {s['written']} files written, "
                f"{s['removed']} removed"
                + (f", {s['deferred']} skipped as too new" if s["deferred"] else "")
                + (f", {bad} unreadable" if bad else "")
                + (f", {rep.errors} errors" if rep.errors else ""))
    if bad:
        rep.summary(f"warning: {bad:,} video{' was' if bad == 1 else 's were'} not a readable MP4 (cut off, empty, or "
                    "still being written?) and skipped; they are tried again on the next run:")
        for path, why in list(ctx.unreadable.items())[:10]:
            rep.summary(f"  {path}: {why}")
        if bad > 10:
            rep.summary(f"  ... and {bad - 10:,} more (all of them are in the log when --log is used)")
    if args.progress:
        wall = time.monotonic() - ctx.started
        other = max(wall - ctx.timing["scan"] - ctx.timing["wait"], 0)
        rep.progress(f"time: {fmt_duration(wall)} in total = {fmt_duration(ctx.timing['scan'])} listing and checking "
                     f"file dates + {fmt_duration(ctx.timing['wait'])} waiting for tag reads + "
                     f"{fmt_duration(other)} everything else (writing files and links); {s['read']:,} videos read")
    if interrupted:
        return 130

    if args.jellyfin_url and args.jellyfin_api_key and not args.dry_run and (s["written"] or s["removed"]):
        trigger_refresh(args.jellyfin_url, args.jellyfin_api_key, rep)
    elif args.jellyfin_url and not args.jellyfin_api_key:
        rep.error("--jellyfin-url needs --jellyfin-api-key (or JELLYFIN_API_KEY)")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
