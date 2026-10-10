# TubeArchivist NFO Writer

Writes Jellyfin-compatible `.nfo` files and artwork for a [TubeArchivist](https://github.com/tubearchivist/tubearchivist) library, using only the metadata TubeArchivist embeds in the MP4 files. No TubeArchivist server, no Jellyfin plugin and no third-party Python packages are needed: just Python 3.

Use it to give stock Jellyfin (with its built-in NFO and local-image support) a proper series/episode layout, or to sort the library without a TubeArchivist server.

```
<library>/<UC channel id>/<video id>.mp4
```

## Quick start (typical use)

1. Put `ta_nfo.py` in its own empty folder, outside the TubeArchivist library, for example `/mnt/media/tanfo`. Use a real copy, not a symlink. **This folder must be on a Linux filesystem that supports symlinks** (see "Where the view can live" below): the default mode fills it with symlinks to your videos.
2. Run it with the TubeArchivist folder as the only argument:

   ```sh
   cd /mnt/media/tanfo
   python3 ta_nfo.py /mnt/media/tubearchivist
   ```

3. The folder that holds the script now has one folder per channel, with readable names, season folders, NFOs, artwork and links to the videos:

   ```
   /mnt/media/tanfo/ta_nfo.py
   /mnt/media/tanfo/<Channel Name>/tvshow.nfo, poster.jpg, …
   /mnt/media/tanfo/<Channel Name>/Season 2026/<yyyymmdd>_[<id>]_<title>.mp4   (link to the TubeArchivist video)
   ```

4. Point Jellyfin (or any program that reads `tvshow.nfo` folders) at `/mnt/media/tanfo`. Mount both folders in the container at the same paths as on the host (see "Recommended setup" below).

The TubeArchivist folder is never written to, so a read-only mount is fine. Run it again whenever you like: only new or changed videos are processed. To run it daily, see "Running it daily from cron".

Other commands:

```sh
python3 ta_nfo.py /mnt/media/tubearchivist --dry-run   # show what would be written
python3 ta_nfo.py /mnt/media/tubearchivist --overwrite # rebuild everything
python3 ta_nfo.py /mnt/media/tubearchivist --cleanup   # also remove links, NFOs and thumbs of deleted videos
```

The tool stops with an error, and writes nothing, if the script folder is inside the library or contains it, or if `ta_nfo.py` is a symlink. For other layouts, see "Other modes and options" below.

## What it writes

These are the files written in every mode. The names below are the in-place names; the default mode writes the same content under readable names (see "View mode details").

| File | Contents |
| --- | --- |
| `<id>.nfo` | title, plot, season, episode (upload time, see "Seasons and episodes"), aired date, runtime, genres, tags, studio, date added, YouTube ID |
| `<id>-thumb.jpg` | episode thumbnail from the `covr` atom |
| `tvshow.nfo` | channel name, description, genres, channel tags, studio, YouTube channel ID |
| `poster.jpg`, `banner.jpg`, `fanart.jpg` | `channel_icon`, `channel_banner`, `channel_tv` atoms |

## Where the metadata comes from

TubeArchivist's **Embed metadata into media file** action writes a `ta` tag holding a JSON copy of its index entry, plus channel artwork, into each MP4. When that tag is present and parseable it is preferred, field by field. Otherwise the standard MP4 atoms are used.

| Field | Source with `ta` | Fallback without `ta` |
| --- | --- | --- |
| Episode title / plot | `title` / `description` | `title` / `description` (`synopsis`) atoms |
| Upload date | `published` (Unix timestamp) | `date` atom |
| Genres | `category` | `genre` atom |
| Tags | `tags` | none |
| Studio and series name | `channel.channel_name` | `artist` atom, then folder name |
| Series plot, series tags | `channel_description`, `channel_tags` | none |
| Series images | `channel_icon`, `channel_banner`, `channel_tv` | none |
| Episode thumbnail | `covr` atom | `covr` atom |

Channel data is taken from the **most recently modified video** in the channel folder.

Both `----` freeform atoms and QuickTime `mdta` keys are read. The `ta` JSON layout is not documented by TubeArchivist; the reader was checked against v0.5.12 files and accepts the document at the root or under a `video`/`data`/`ta`/`metadata` object.

## Seasons and episodes

Jellyfin identifies a season by an integer, so a separate season per video type is done with numbers:

| Video type (`ta` `vid_type`) | Season | Example |
| --- | --- | --- |
| videos (default) | upload year | 2026 |
| shorts | year + 10000 | 12026 |
| streams | year + 20000 | 22026 |

The episode number is the upload hour (UTC) as `MMDDHH`, written as an integer: a video published on 11 September 2026 at 14:30 is season 2026, episode `91114`. Episodes sort chronologically within a season, and the number comes from the video's own timestamp, so it needs no knowledge of the other videos and stays the same across incremental runs. The month has no leading zero (an integer cannot hold one), while the day and hour are always two digits, so January 5 at 03:00 is `10503` and October 1 at 00:00 is `100100`. Videos uploaded in the same hour share an episode number. That is accepted, because it is rare and Jellyfin still lists each video. A video whose metadata has only a date and no time of day (the plain MP4 `date` atom) gets `MMDD00`. Dates are UTC. Without the `ta` tag the video type is unknown, so everything goes in the year's regular season. The numbers are the same in both modes. View mode (the default) also gives each season a `Season N` folder and a `season.nfo` with a readable title ("2026", "2026 Shorts", "2026 Streams"), which Jellyfin shows instead of "Season 12026" (checked on stock Jellyfin with the plugin off). In `--in-place` mode there are no season folders or titles, so stock Jellyfin shows "Season 12026".

## Where the view can live

**The default (view) mode is built from symlinks to the original videos, so the view folder needs a filesystem that supports them.** This is a hard requirement, not a tuning choice.

| Where the view folder is | Works? |
| --- | --- |
| Local Linux disk: ext4, xfs, btrfs, ZFS | Yes. This is the intended setup. |
| An SMB/CIFS share mounted on Linux (typically mounted `nounix`) | **No.** Creating a symlink fails with "Operation not supported" (error 95). |
| exFAT or FAT disk | **No.** Neither symlinks nor hard links exist there. |
| NTFS | Not reliably. Treat it as unsupported. |
| Windows | **Not supported.** The links hold Linux paths, so programs on Windows cannot follow them, and Windows symlinks need extra privileges. Run the tool on Linux. |

What this means in practice:
- **The library can stay on an SMB share.** The tool only reads it, so the TubeArchivist folder can be a network mount, even read-only. Only the **view** must be on a local Linux disk. Put the view on the machine that runs the tool, or on the machine that runs Jellyfin, and bind-mount it into the container.
- **The tool checks first.** If it cannot create a symlink in the view folder, it stops at once with one clear error, before writing anything else.
- **Network shares also write slowly.** The view holds thousands of small files, so a local disk is faster as well as being the only place the links work.
- **Alternatives when the view must be on a file server:**
  - Run the tool on the file server itself, against its local paths. The links are then made on the server's own filesystem. They resolve for network clients only if the view and the library are under the same share and the server's settings allow following symlinks. I have not verified that.
  - `--link-type hard`: hard links need no symlink support, but the view and the library must be on the same filesystem, and the filesystem must support them. A hard-linked video keeps its data on disk after TubeArchivist deletes it, until `--cleanup` removes the link.
  - `--in-place` mode writes NFOs next to the videos and uses no links at all. It needs a writable library and gives ID-only names.

## View mode details

View mode is the default, the one the quick start uses. It builds a **separate library** in the folder that holds `ta_nfo.py` and never touches the TubeArchivist folder.

```
<view>/<Channel Name>/
  tvshow.nfo  poster.jpg  banner.jpg  fanart.jpg
  Season 2026/
    season.nfo
    20260911_[<id>]_<Title>.mp4      -> symlink to the TubeArchivist video
    20260911_[<id>]_<Title>.nfo
    20260911_[<id>]_<Title>-thumb.jpg
    20260911_[<id>]_<Title>.en.vtt   -> symlink (when TubeArchivist has one)
  Season 12026/   (shorts)     Season 22026/   (streams)
```

### Recommended setup: the same paths on the host and in every container

Symlink targets are absolute paths. A link resolves wherever its target path exists, so mount **both** folders at the **same absolute paths** as on the host, read-only:

```
/mnt/media/tubearchivist   (TubeArchivist library)
/mnt/media/tanfo           (the view: the folder that holds ta_nfo.py)
```

```yaml
volumes:
  - /mnt/media/tubearchivist:/mnt/media/tubearchivist:ro
  - /mnt/media/tanfo:/mnt/media/tanfo:ro
```

- The links then work on the host and in any number of containers. Nothing is made per container, and you do not need `--link-root`.
- Every container must mount **both** folders. With only the view, the links dangle. With only the TubeArchivist folder, there are no NFOs.
- Jellyfin library path: `/mnt/media/tanfo`.
- Alternative: `--link-type hard` with both folders on one filesystem. Then a container needs only the view folder.
- Use `--link-root` only when a container cannot use the host path. Links then look broken on the host.

Rules:
- **First name wins.** Jellyfin tracks items by path, so once a link or channel folder exists the tool never renames it, even if the title or channel name changes later. The NFO inside is still updated. The tool finds existing names from the files themselves (the `[<id>]` in the name, and the channel id inside `tvshow.nfo`). Links made with the older `2026-09-11 - <Title> [<id>]` format are still recognised and keep their names; there is no state file.
- Two channels with the same name: the second gets ` [<channel id>]` added.
- Names drop `/ \ : * ? " < > |` and control characters, and are cut to 120 bytes. The title part of a video name is cut to 64 characters (the NFO keeps the full title); the `[<id>]` keeps names unique. A video with no date goes in `Season 0` as `undated_[<id>]_<Title>`. The date and the video ID come first, so names sort by date and a long title is the only part that gets cut.
- Freshness, `--min-age`, the channel refresh gate, `--dry-run`, `--overwrite`, `--workers`, exit codes and the Jellyfin rescan work as in `--in-place` mode. Timestamps are taken from the TubeArchivist file, not the link.
- `--cleanup` removes view links, NFOs and thumbnails whose video is gone from TubeArchivist, then season folders that are left with nothing but `season.nfo`. It leaves any other file alone, including a real file whose name looks like a link. Like `--in-place` mode, it skips a channel with no videos. It does not remove channel folders.
- The season folder name stays `Season N` because Jellyfin reads the number from it, and the readable name lives in `season.nfo`. Stock Jellyfin shows that name ("2026 Shorts", "2019 Streams").

## Other modes and options

| Option | Meaning |
| --- | --- |
| `--view-dir DIR` | Build the view in DIR instead of the script folder. Must be outside the library. |
| `--in-place` | Skip view mode and write NFOs and artwork next to the videos, inside the TubeArchivist folder (it must be writable). Gives ID-only names and no season folders. Cannot be combined with the view options. |
| `--view-library NAME` | Put the channels in a folder of this name inside the view. Default: none, the channels sit directly in the view. Use it when a program expects a fixed top folder such as `tvseries`. |
| `--link-type symlink\|hard` | Default `symlink`. `hard` needs the view on the same filesystem as the library, and keeps the data on disk after TubeArchivist deletes a video. A container then needs only the view folder. |
| `--workers N` | Parallel file reads and file-date checks (default 16). On a network share nearly all the time is spent waiting for the network, so more is faster; see "Large libraries and slow shares". |
| `--progress` | Print a line per channel and a heartbeat every 30 seconds with the rate and an estimate of the time left, then a breakdown of where the time went. Shown even with `--quiet`. |
| `--version` | Print the version and exit. Useful for checking which copy of the script you are running. |
| `--playlists` | Also write one `.m3u8` playlist per TubeArchivist playlist into `<view>/Playlists/`. See "Playlists". View mode only. |
| `--link-root PATH` | Write symlink targets as `PATH/<channel id>/<id>.mp4`. Only for a container that cannot mount the library at the host path. The links are then valid inside that container only. |

## Playlists

With `--playlists`, the tool also writes one standard `.m3u8` playlist file per TubeArchivist playlist, using the playlist data TubeArchivist embeds in each video:

```
/mnt/media/tanfo/Playlists/<Playlist Name>.m3u8
```

```
#EXTM3U
#PLAYLIST:<Playlist Name>
# ta_nfo.py playlist_id=TA_playlist_… refreshed=…
#EXTINF:-1,<Title>
../<Channel Name>/Season 2026/20260911_[<id>]_<Title>.mp4
```

- The files list only videos that exist in the view, in the playlist's order, with paths relative to the `Playlists` folder. Any program that reads `.m3u8` files can use them, for example VLC or Kodi. Jellyfin does not turn them into its own playlists.
- The data is a snapshot taken when TubeArchivist last embedded each video. A change to a playlist in TubeArchivist reaches the file after its videos are re-embedded, because only videos that are new or changed are read. The first `--playlists` run on a view reads every video once to find the playlists. That scan is recorded channel by channel in `Playlists/.scanned` and each channel's playlists are written as soon as the channel is done, so an interrupted scan carries on with the unfinished channels instead of starting over. A finished video is read again only for its small `ta` tag, not its thumbnail. A `Playlists` folder without a `.scanned` file comes from an older version that finished its scan, and counts as done.
- Playlist file names follow the first-name-wins rule. Two playlists with the same name get a short ID added. A channel named `Playlists` is given an ID suffix so the two do not clash.
- `--cleanup` also removes entries whose video link is gone from files this tool wrote, and removes a playlist left empty. It never touches `.m3u8` files it did not write.
- Off by default, and not available with `--in-place`.

## Large libraries and slow shares

The first run reads every video once. That is mostly waiting for the network: each video takes about 14 small reads and 120 KB of data for a typical TubeArchivist file, and each read is a round trip to the share. So:

- **Use `--progress` for a big first run.** It prints the total up front, a line per channel, a heartbeat every 30 seconds with the rate and an estimate of the time left, and at the end a breakdown: time listing and checking file dates, time waiting for tag reads, and time spent writing files and links. Within a minute you know whether the run will take minutes or hours. Add `--quiet` to hide the per-file lines, which for hundreds of thousands of videos are only noise: `python3 ta_nfo.py /path/to/library --progress --quiet`.
- **Raise `--workers` if the time goes on "waiting for tag reads".** The default is 16. A share that answers slowly keeps getting faster with more, until the server or the network is the limit: try `--workers 32` or `64` and watch the videos-per-second rate. In a simulation with a 60 ms delay per read, 4 workers (the old default) did about 9 videos per second, 16 about 37, and 32 about 70. Each worker holds a file open on the share, so stop going up if the server starts to struggle or to refuse connections.
- **Stopping is safe.** Press Ctrl-C and the tool finishes the files it is writing, prints a summary and exits with status 130. Every file is written atomically, and the next run carries on: a video is read again only if its NFO is missing or older than the video, so finished videos cost one file-date check each. A channel folder is recognised from its `tvshow.nfo`, which is written before the channel's first episode, so a stopped run does not leave a duplicate `Channel [ID]` folder behind.
- **Do the big first run without `--playlists` if you can.** Then add `--playlists` for a second pass: it reads each finished video only for its playlists, and that pass is recorded per channel and resumes too.

## Running it daily from cron

```cron
0 4 * * *  flock -n /tmp/ta_nfo.lock python3 /mnt/media/tanfo/ta_nfo.py /mnt/media/tubearchivist --cleanup --quiet --log /var/log/ta_nfo.log
```

| Behaviour | Detail |
| --- | --- |
| Incremental | A video is read only when its `.nfo` is missing or older than the `.mp4`. Unchanged files cost one `stat()` each (3,000 unchanged videos take about 0.3 s). |
| Skips files still being written | Videos modified in the last `--min-age` minutes (default 10) are left for the next run. |
| Channel info is rebuilt rarely | `tvshow.nfo` and the channel images are rebuilt only when a video was added or changed since they were written **and** they are at least `--channel-refresh-days` old (default 183, about six months). A missing `tvshow.nfo` is always created. |
| Incomplete channel info is fixed early | If `tvshow.nfo` has no description or there is no poster (typically written before TubeArchivist embedded its tags), it is rebuilt as soon as a video is re-embedded, regardless of age. |
| `--cleanup` | Removes the view's links, NFOs and thumbnails whose video is gone (in `--in-place` mode: `<id>.nfo` / `<id>-thumb.*` whose `.mp4` is gone). Only removes files the tool writes (the `.nfo` must carry a matching YouTube `uniqueid`), never `tvshow.nfo`, channel images or `.vtt` subtitles, and skips any channel folder with no videos so an unmounted share cannot trigger mass deletion. |
| Ownership | New files copy the video's permissions and, when allowed, its owner and group, so Jellyfin can read them. Disable with `--no-match-owner`. Only root can change ownership; otherwise files belong to the cron user. |
| Safe writes | Files are written to a temp name and renamed into place. |
| Exit status | `1` if any file could not be read, written, removed, or the Jellyfin refresh failed; `0` otherwise. Errors go to stderr (cron mails them). A video that cannot be read (cut off, empty, or still being written) is retried once, then reported once with its size and age and skipped; the rest of the run carries on. It keeps the exit status at 1 until the file is fixed or removed. `--quiet` prints only errors and the summary; `--log FILE` also appends to a file. |
| Parallel reads | `--workers N` (default 16) reads files and checks file dates in parallel, and the reads run ahead of the writing. This mainly helps on slow network storage; see "Large libraries and slow shares". |
| Optional Jellyfin rescan | `--jellyfin-url` and `--jellyfin-api-key` (or `JELLYFIN_URL` / `JELLYFIN_API_KEY`) ask Jellyfin to refresh its library after a run that changed files. Not yet tried against a real Jellyfin server. |

Freshness is based on modification times, so a restore or copy that gives files new timestamps makes the next run rebuild them; `cp -p` and `rsync -t` preserve them. Re-running TubeArchivist's embed action also triggers a rebuild, which is what you want.

## Tests

```sh
python3 -m unittest -v
```

The tests build tiny synthetic MP4 containers, so no video files or ffmpeg are needed.

## Related

A separate project, Jelly_TA_MetaReader, is a Jellyfin plugin that reads the same embedded metadata directly, without writing any files. Use one or the other on a library, not both.

## Example: Jellyfin in Docker

A complete setup for stock Jellyfin (no plugin) reading the view. It was checked with the `lscr.io/linuxserver/jellyfin` image. The paths are examples; use your own, but keep each one identical on the host and in the container.

**1. Create the view and run the script on the host**

```sh
mkdir -p /mnt/media/tanfo
cp ta_nfo.py /mnt/media/tanfo/
cd /mnt/media/tanfo
python3 ta_nfo.py /mnt/media/tubearchivist
```

The source path must be the host path you mount in step 2.

**2. `docker-compose.yml`**

```yaml
services:
  jellyfin:
    image: lscr.io/linuxserver/jellyfin
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    volumes:
      - jellyfin:/config
      # Mount both folders at the SAME path as on the host, read-only.
      # The links in the view are absolute, so they only resolve at these paths.
      - /mnt/media/tubearchivist:/mnt/media/tubearchivist:ro
      - /mnt/media/tanfo:/mnt/media/tanfo:ro
    ports:
      - 8096:8096
    restart: unless-stopped

volumes:
  jellyfin:
```

Create both host folders before the first `docker compose up -d`. If one is missing, Docker creates an empty root-owned folder there and the links dangle. After changing the mounts, run `docker compose up -d` again to recreate the container. The `PUID` user needs read access to both folders.

**3. Jellyfin library settings**

1. Add a library of type **Shows** and set its folder to `/mnt/media/tanfo`.
2. Turn off every online metadata downloader (TheTVDB, TMDb and so on), so only the NFO data is used.
3. Keep the NFO reader and local images enabled. Turn off "save metadata as NFO".
4. Scan the library.

**4. Check that the links resolve inside the container**

```sh
docker exec <container name> ls -lL "/mnt/media/tanfo/<Channel Name>/Season 2026/"
```

The `.mp4` should show a file size. "No such file" means the container is missing a mount, or the script was run with a different source path than the one you mounted.

**What to expect.** One series per channel, seasons by upload year (with separate "Shorts" and "Streams" seasons), full titles and descriptions, artwork, genres, tags and subtitles. Watch state survives rerunning the script, because existing file and folder names are never changed.
