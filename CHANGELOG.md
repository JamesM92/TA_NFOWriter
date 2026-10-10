# Changelog

## [Unreleased]

## [0.5.2] - 2026-10-10

### Added
- A video that is still being downloaded or copied is recognised by its size, not its date. If a video is not a usable MP4 and its size changed between the tool's two attempts to read it, it is skipped quietly (no warning, counted under "skipped as too new") and picked up on a later run. The date check (`--min-age`) can miss such a file when it was moved in from a cache and kept its old date, which used to produce a warning about an unreadable video.

## [0.5.1] - 2026-10-10

### Fixed
- **An unreadable newest video no longer blocks its channel.** The series info (`tvshow.nfo` and the artwork) comes from the channel's newest video. When that video could not be read, for example a half-downloaded upload, a new channel was skipped completely on every run, and an existing channel's series info stopped updating. The next few videos are now tried, so the channel is built from the newest one that can be read.
- The same unreadable video is reported once per run, not once for each step that touched it (in-place mode reported it twice).

### Changed
- **A video that is not a usable MP4 is now a warning, not an error.** A cut-off, empty or half-downloaded file used to make the whole run exit 1, so one permanently broken video kept cron and monitoring red on every run and could hide a real failure. Such a video is now skipped with a `warning:` line, listed in a recap at the end (and counted as "unreadable" in the summary line), and tried again next run; the exit status stays 0. These are still errors with exit 1: a file that cannot be opened at all (permission denied, I/O error, the share gone), and too many unreadable videos (at least 10 and more than 5 percent of those read), which points at a wrong folder or a sick share.
- A video whose read fails is retried once after a short pause before it is reported. A busy network share can return a short read or an error once, which looked like a corrupt file ("no moov box").
- The unreadable-video message now says what was found: the file size or that it is empty, and how long ago it was last modified.

## [0.5.0] - 2026-10-08

### Added
- `--progress`: the total up front, a line per channel, a heartbeat every 30 seconds with the rate and an estimate of the time left, and at the end a breakdown of where the time went (listing and file dates, waiting for tag reads, everything else). Shown even with `--quiet`.
- Ctrl-C stops cleanly: the tool prints a summary and exits with status 130, and the next run carries on.

### Changed
- **Much faster first runs on a network share.** The default for `--workers` is now 16 (it was 4). Tag reads run ahead of the writing instead of in batches that the writer waits for, the file dates of a channel's videos are checked in parallel, files are opened with a random-access hint so the kernel does not read ahead megabytes of a file we jump around in, and a new channel's newest video is opened once instead of three times. In a simulation with a 60 ms delay per network read, the same library went from about 9 videos per second to about 37 with the default and about 70 with `--workers 32`.
- The `--playlists` scan of videos that are already finished reads only their small `ta` tag, not the 60 to 90 KB thumbnail.

### Fixed
- An interrupted first `--playlists` scan no longer starts over. The scan is recorded per channel in `Playlists/.scanned`, and each channel's playlists are written when the channel is done. Before, the only record was a folder created at the very end, so a stopped scan re-read every video and lost the playlist data it had collected. A `Playlists` folder from an older version, without that file, counts as a finished scan.

## [0.4.2] - 2026-10-05

### Fixed
- View mode writes `tvshow.nfo` as soon as a channel folder is made, before any episode. A run that stops partway (a server reboot, for example) leaves a folder the next run recognises and finishes, instead of a half-built folder that gets a duplicate `Channel [ID]` beside it.

## [0.4.1] - 2026-10-04

### Fixed
- View mode no longer builds a second `Channel [ID]` folder next to an empty `Channel` folder. An empty folder, such as one left by a run that stopped before writing anything, is reused. A folder with anything in it still counts as taken.

## [0.4.0] - 2026-10-04

### Added
- `--version` prints the version of the script.
- A check before any file is written that the view folder can hold symlinks. If it cannot (SMB/CIFS mounts, exFAT/FAT), the tool stops with one clear error and suggests the alternatives.
- README: "Where the view can live", with the filesystem requirements of view mode.

### Changed
- **Episode numbers are now the upload hour, `MMDDHH` (UTC), instead of the day of the year.** Episodes sort chronologically within a season, and videos uploaded on different days or in different hours get different numbers. Example: a video published at 2026-09-11 14:30 UTC is season 2026, episode 91114. Uploads in the same hour share a number. Seasons (year, plus 10000 for shorts and 20000 for streams) are unchanged. Existing NFOs keep their old numbers until they are rewritten, so run once with `--overwrite` after upgrading and refresh the metadata in Jellyfin.

### Fixed
- Memory use no longer grows with the number of videos in a channel. A first run used about 0.9 MB of RAM per video (each video's thumbnail plus the three channel images TubeArchivist embeds in every video), so a channel with thousands of videos needed gigabytes. Videos are now read, written and released 100 at a time, and the channel images are read only when the channel info is written, from the newest video (older ones only for artwork it lacks). Peak memory is about 2 MB regardless of channel size (measured: 361 MB before and 2 MB after, for 400 videos). Output is unchanged. Unchanged channels also no longer read the channel images on every run.

## [0.3.0] - 2026-10-04

### Added
- `--playlists` (view mode): writes one `.m3u8` per TubeArchivist playlist into `<view>/Playlists/`, from the playlists embedded in the videos. Off by default.
- README: a full Jellyfin-in-Docker example.

## [0.2.0] - 2026-10-04

### Changed
- The title part of view file names is cut to 64 characters (the NFO keeps the full title).
- View mode file names are now `YYYYMMDD_[<id>]_<Title>.mp4` (and `undated_[<id>]_<Title>`), so the date and video ID come first. Links in the older `YYYY-MM-DD - <Title> [<id>]` format are still recognised and keep their names.
- **Breaking:** view mode is now the default. The view is built in the folder that holds `ta_nfo.py` (channel folders sit directly there, no `tvseries` level unless `--view-library NAME` is given; override the location with `--view-dir`; a symlinked script is rejected when the default is used), so the TubeArchivist folder can be read only. The old behaviour of writing beside the videos needs `--in-place`.
- Docs and help text: the recommended view-mode setup is the same absolute paths on the host and in every container; `--link-root` is only for containers that cannot do that.

### Added
- View mode (`--view-dir`, `--view-library`, `--link-type`, `--link-root`, `--in-place` for the old behaviour): builds a separate library with readable names, `Season N` folders and symlinks (or hard links) to the videos, with NFOs and artwork written there instead of into the TubeArchivist folder.
- Exit status 1 with an error message when the library folder contains no channel folders (`UC` plus 22 characters), so a wrong path or unmounted share is not reported as success.
- Lint and test CI on `dev` and `main` pushes (`lint.yml`).

## [0.1.0]

- First version: reads TubeArchivist's embedded MP4 metadata and writes Jellyfin `.nfo` files and artwork.
