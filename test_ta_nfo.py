"""Tests for ta_nfo.py. Run with:  python3 -m unittest -v

Fixtures are tiny hand-built MP4 containers (ftyp/moov/udta/meta/ilst/mdat), so no real video or
ffmpeg is needed.
"""
import collections
import contextlib
import datetime
import http.server
import io
import json
import shutil
import os
import re
import struct
import sys
import tempfile
import threading
import tracemalloc
import time
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ta_nfo  # noqa: E402
ta_nfo.RETRY_DELAY = 0  # tests do not need the pause before the retry of a failed read

CHAN = "UCeY0bbntWzzVIaj2z3QigXg"
JPEG = b"\xff\xd8\xff\xe0" + b"fakejpeg"
DAY = 86400


def box(typ, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def data_box(value: bytes, flags=1):
    return box(b"data", struct.pack(">II", flags, 0) + value)


def text_atom(name, value):
    return box(name.encode("latin-1"), data_box(value.encode()))


def freeform(name, value: bytes, flags=1):
    return box(b"----", box(b"mean", b"\0\0\0\0com.tubearchivist") + box(b"name", b"\0\0\0\0" + name.encode())
               + data_box(value, flags))


def mp4_bytes(items=(), mdta=None):
    """ilst layout by default; pass mdta={'ta': b'...'} for the QuickTime keys/ilst layout."""
    hdlr = box(b"hdlr", b"\0" * 8 + b"mdir" + b"\0" * 12)
    if mdta is not None:
        names = list(mdta)
        keys = box(b"keys", struct.pack(">II", 0, len(names)) + b"".join(box(b"mdta", n.encode()) for n in names))
        # ilst items are boxes whose type is the 1-based index into the keys box
        ilst = box(b"ilst", b"".join(box(struct.pack(">I", i + 1), data_box(v)) for i, v in enumerate(mdta.values())))
        meta = box(b"meta", b"\0\0\0\0" + box(b"hdlr", b"\0" * 8 + b"mdta" + b"\0" * 12) + keys + ilst)
    else:
        meta = box(b"meta", b"\0\0\0\0" + hdlr + box(b"ilst", b"".join(items)))
    return (box(b"ftyp", b"isom\0\0\2\0isomiso2mp41") + box(b"moov", box(b"mvhd", b"\0" * 100) + box(b"udta", meta))
            + box(b"mdat", b"\0" * 64))


def ta_json(_playlists=None, **video):
    base = {"youtube_id": "x", "title": "TA title", "description": "TA plot", "published": 1789137011,
            "category": ["News & Politics"], "tags": ["a", "b"], "vid_type": "videos",
            "date_downloaded": 1791114612, "player": {"duration": 282},
            "channel": {"channel_id": CHAN, "channel_name": "NBC News", "channel_description": "About NBC",
                        "channel_tags": ["news"]}}
    base.update(video)
    return json.dumps({"video": base, "comments": None, "playlists": _playlists, "version": "v0.5.12"}).encode()


def full_items(_playlists=None, **video):
    return [text_atom("©nam", "Atom title"), text_atom("©ART", "Atom artist"), text_atom("©day", "20200808"),
            text_atom("©gen", "Atom genre"), text_atom("desc", "Atom plot"),
            box(b"covr", data_box(JPEG, 13)),
            freeform("ta", ta_json(_playlists, **video)),
            freeform("channel_icon", JPEG, 13), freeform("channel_banner", JPEG, 13),
            freeform("channel_tv", JPEG, 13)]


def plain_items():
    return [text_atom("©nam", "Atom title"), text_atom("©ART", "Atom artist"), text_atom("©day", "20200808"),
            text_atom("©gen", "Atom genre"), text_atom("desc", "Atom plot")]


def run(lib, *args, auto_mode=True):
    """Run main(). By default adds --in-place unless a view dir is given; auto_mode=False passes args as-is."""
    if auto_mode and "--view-dir" not in args and "--in-place" not in args:
        args = ("--in-place", *args)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ta_nfo.main([str(lib), "--min-age", "0", *args])
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lib = Path(self._tmp.name)
        self.ch = self.lib / CHAN
        self.ch.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def video(self, vid, content, age_days=0):
        p = self.ch / f"{vid}.mp4"
        p.write_bytes(content)
        if age_days:
            t = time.time() - age_days * DAY
            os.utime(p, (t, t))
        return p

    def age(self, name, days):
        t = time.time() - days * DAY
        os.utime(self.ch / name, (t, t))

    def age_channel_files(self, days):
        for n in ("tvshow.nfo", "poster.jpg", "banner.jpg", "fanart.jpg"):
            self.age(n, days)


class ReadingTests(Base):
    def test_ta_wins_over_atoms(self):
        p = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        m = ta_nfo.read(p)
        self.assertEqual(m["title"], "TA title")
        self.assertEqual(m["artist"], "NBC News")
        self.assertEqual(m["plot"], "TA plot")
        self.assertEqual(m["genres"], ["News & Politics"])
        self.assertEqual(m["date"].isoformat(), "2026-09-11")  # epoch 1789137011, UTC
        self.assertEqual(m["chan_desc"], "About NBC")

    def test_falls_back_to_atoms_without_ta(self):
        m = ta_nfo.read(self.video("aaaaaaaaaaa", mp4_bytes(plain_items())))
        self.assertEqual((m["title"], m["artist"], m["plot"]), ("Atom title", "Atom artist", "Atom plot"))
        self.assertEqual(m["date"].isoformat(), "2020-08-08")
        self.assertEqual(m["genres"], ["Atom genre"])
        self.assertIsNone(m["chan_desc"])

    def test_malformed_ta_falls_back(self):
        m = ta_nfo.read(self.video("aaaaaaaaaaa", mp4_bytes(plain_items() + [freeform("ta", b"{not json")])))
        self.assertEqual(m["title"], "Atom title")

    def test_partial_ta_fills_only_what_it_has(self):
        partial = json.dumps({"video": {"youtube_id": "x", "channel": {"channel_name": "Chan"}}}).encode()
        m = ta_nfo.read(self.video("aaaaaaaaaaa", mp4_bytes(plain_items() + [freeform("ta", partial)])))
        self.assertEqual((m["title"], m["artist"]), ("Atom title", "Chan"))

    def test_wrapper_object_and_root_document(self):
        doc = json.dumps({"title": "Root title"}).encode()
        m = ta_nfo.read(self.video("aaaaaaaaaaa", mp4_bytes([freeform("ta", doc)])))
        self.assertEqual(m["title"], "Root title")

    def test_mdta_layout(self):
        m = ta_nfo.read(self.video("aaaaaaaaaaa", mp4_bytes(mdta={"ta": ta_json()})))
        self.assertEqual(m["title"], "TA title")

    def test_image_extension_detected_from_content(self):
        self.assertEqual(ta_nfo.image_ext(b"\x89PNG\r\n\x1a\nxxxx"), ".png")
        self.assertEqual(ta_nfo.image_ext(b"RIFF\0\0\0\0WEBPxx"), ".webp")
        self.assertEqual(ta_nfo.image_ext(JPEG), ".jpg")

    def test_episode_numbers_are_the_upload_hour(self):
        utc = datetime.timezone.utc
        d = datetime.date(2026, 9, 11)
        moment = datetime.datetime(2026, 9, 11, 14, 50, 11, tzinfo=utc)
        self.assertEqual(ta_nfo.episode_number(d, moment), 91114)
        self.assertEqual(ta_nfo.episode_number(d), 91100)  # date only: no time of day
        last = datetime.datetime(2026, 12, 31, 23, 59, 59, tzinfo=utc)
        self.assertEqual(ta_nfo.episode_number(last.date(), last), 123123)
        # the month has no leading zero (an integer cannot hold one); day and hour are always two digits
        jan = datetime.datetime(2026, 1, 5, 3, 7, tzinfo=utc)
        self.assertEqual(ta_nfo.episode_number(jan.date(), jan), 10503)
        oct_ = datetime.datetime(2026, 10, 1, 0, 5, tzinfo=utc)
        self.assertEqual(ta_nfo.episode_number(oct_.date(), oct_), 100100)
        # chronological within a year, across months and within a day
        times = [datetime.datetime(2026, m, dd, h, 0, 0, tzinfo=utc)
                 for m, dd, h in ((1, 31, 23), (2, 1, 0), (2, 1, 1), (12, 31, 23))]
        numbers = [ta_nfo.episode_number(t.date(), t) for t in times]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(len(set(numbers)), len(numbers))

    def test_uploads_in_different_hours_differ_and_the_same_hour_shares_a_number(self):
        # 1789137011 is 2026-09-11 14:30:11 UTC; +90 s is 14:31:41 (same hour); +3600 s is 15:30:11
        self.video("aaaaaaaaaaa", mp4_bytes(full_items(published=1789137011)))
        self.video("bbbbbbbbbbb", mp4_bytes(full_items(published=1789137101)))
        self.video("ccccccccccc", mp4_bytes(full_items(published=1789137011 + 3600)))
        run(self.lib, "--in-place")
        episode = {v: re.search(r"<episode>(\d+)</episode>", (self.ch / f"{v}.nfo").read_text())[1]
                   for v in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc")}
        self.assertEqual(episode, {"aaaaaaaaaaa": "91114", "bbbbbbbbbbb": "91114", "ccccccccccc": "91115"})
        self.assertIn("<season>2026</season>", (self.ch / "aaaaaaaaaaa.nfo").read_text())

    def test_season_numbers(self):
        import datetime
        d = datetime.date(2026, 9, 11)
        self.assertEqual(ta_nfo.season_number(d, "videos"), 2026)
        self.assertEqual(ta_nfo.season_number(d, None), 2026)
        self.assertEqual(ta_nfo.season_number(d, "shorts"), 12026)
        self.assertEqual(ta_nfo.season_number(d, "streams"), 22026)


class OutputTests(Base):
    def test_first_run_writes_everything(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        code, out, _ = run(self.lib)
        self.assertEqual(code, 0)
        for n in ("aaaaaaaaaaa.nfo", "aaaaaaaaaaa-thumb.jpg", "tvshow.nfo", "poster.jpg", "banner.jpg", "fanart.jpg"):
            self.assertTrue((self.ch / n).exists(), n)
        nfo = (self.ch / "aaaaaaaaaaa.nfo").read_text()
        self.assertIn("<season>2026</season>", nfo)
        self.assertIn("<runtime>5</runtime>", nfo)

    def test_shorts_and_streams_get_their_own_seasons(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items(vid_type="shorts")))
        self.video("bbbbbbbbbbb", mp4_bytes(full_items(vid_type="streams")))
        run(self.lib)
        self.assertIn("<season>12026</season>", (self.ch / "aaaaaaaaaaa.nfo").read_text())
        self.assertIn("<season>22026</season>", (self.ch / "bbbbbbbbbbb.nfo").read_text())

    def test_second_run_is_a_no_op(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()), age_days=5)
        run(self.lib)
        _, out, _ = run(self.lib)
        self.assertIn("0 files written", out)

    def test_changed_video_rebuilds_its_nfo(self):
        p = self.video("aaaaaaaaaaa", mp4_bytes(full_items()), age_days=5)
        run(self.lib)
        self.age("aaaaaaaaaaa.nfo", 3)
        os.utime(p, None)  # re-embedded: mtime moves past the nfo
        _, out, _ = run(self.lib)
        self.assertIn("aaaaaaaaaaa.nfo", out)

    def test_corrupt_file_is_a_warning_and_does_not_stop_the_rest(self):
        self.video("aaaaaaaaaaa", b"not an mp4 at all")
        self.video("bbbbbbbbbbb", mp4_bytes(full_items()))
        code, _, err = run(self.lib)
        self.assertEqual(code, 0)  # a broken video is a warning, not a failed run
        self.assertIn("warning: cannot read", err)
        self.assertNotIn("error:", err)
        self.assertIn("aaaaaaaaaaa.mp4", err)
        self.assertFalse((self.ch / "aaaaaaaaaaa.nfo").exists())  # no empty NFO for an unreadable file
        self.assertTrue((self.ch / "bbbbbbbbbbb.nfo").exists())

    def test_truncated_file_is_reported(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items())[:60])
        code, out, err = run(self.lib)
        self.assertEqual(code, 0)
        self.assertIn("warning: cannot read", err)
        self.assertIn("1 unreadable", out)  # in the summary line
        self.assertIn("warning: 1 video was not a readable MP4", out)  # and a recap at the end
        self.assertIn("aaaaaaaaaaa.mp4", out)

    def test_valid_mp4_with_no_tags_is_not_an_error(self):
        self.video("aaaaaaaaaaa", mp4_bytes([text_atom("©too", "Lavf63.6.100")]))
        code, _, _ = run(self.lib)
        self.assertEqual(code, 0)
        self.assertTrue((self.ch / "aaaaaaaaaaa.nfo").exists())

    def test_unwritable_target_gives_nonzero_exit(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        (self.ch / "aaaaaaaaaaa.nfo").mkdir()  # a directory where the file should go
        code, _, err = run(self.lib, "--overwrite")
        self.assertEqual(code, 1)
        self.assertIn("error", err)

    def test_dry_run_writes_nothing(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        run(self.lib, "--dry-run")
        self.assertFalse((self.ch / "aaaaaaaaaaa.nfo").exists())

    def test_log_file_and_quiet(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        log = self.lib / "run.log"
        _, out, _ = run(self.lib, "--quiet", "--log", str(log))
        self.assertEqual(out.strip().count("\n"), 0)  # summary only
        self.assertIn("write", log.read_text())

    def test_no_temp_files_left_behind(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        run(self.lib)
        self.assertEqual([p.name for p in self.ch.iterdir() if p.name.endswith(".tmp")], [])

    def test_new_files_copy_video_mode(self):
        p = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        os.chmod(p, 0o640)
        run(self.lib)
        self.assertEqual((self.ch / "aaaaaaaaaaa.nfo").stat().st_mode & 0o777, 0o640)


class MinAgeTests(Base):
    def test_recent_videos_are_skipped_then_picked_up(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ta_nfo.main([str(self.lib), "--in-place", "--min-age", "10"])
        self.assertFalse((self.ch / "aaaaaaaaaaa.nfo").exists())
        self.assertIn("skipped as too new", out.getvalue())
        self.age("aaaaaaaaaaa.mp4", 1)
        run(self.lib)
        self.assertTrue((self.ch / "aaaaaaaaaaa.nfo").exists())


class ChannelGateTests(Base):
    def setUp(self):
        super().setUp()
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()), age_days=300)
        run(self.lib)
        self.age("aaaaaaaaaaa.nfo", 250)
        self.age("aaaaaaaaaaa-thumb.jpg", 250)

    def new_video(self, vid="bbbbbbbbbbb"):
        self.video(vid, mp4_bytes(full_items()))

    def test_old_channel_without_new_video_is_left_alone(self):
        self.age_channel_files(200)
        _, out, _ = run(self.lib)
        self.assertNotIn("tvshow.nfo", out)

    def test_new_video_but_channel_under_six_months_old(self):
        self.age_channel_files(100)
        self.new_video()
        _, out, _ = run(self.lib)
        self.assertIn("bbbbbbbbbbb.nfo", out)
        self.assertNotIn("tvshow.nfo", out)

    def test_new_video_and_channel_over_six_months_old(self):
        self.age_channel_files(200)
        self.new_video()
        _, out, _ = run(self.lib)
        self.assertIn("tvshow.nfo", out)

    def test_missing_tvshow_is_always_created(self):
        (self.ch / "tvshow.nfo").unlink()
        _, out, _ = run(self.lib)
        self.assertIn("tvshow.nfo", out)

    def test_refresh_days_flag(self):
        self.age_channel_files(100)
        self.new_video()
        _, out, _ = run(self.lib, "--channel-refresh-days", "50")
        self.assertIn("tvshow.nfo", out)

    def test_incomplete_channel_info_is_rebuilt_early(self):
        # Channel info written before the tags were embedded: no plot, no poster.
        (self.ch / "tvshow.nfo").write_text("<tvshow><title>x</title></tvshow>")
        for n in ("poster.jpg", "banner.jpg", "fanart.jpg"):
            (self.ch / n).unlink()
        self.age("tvshow.nfo", 10)  # young, so the six-month gate alone would block it
        self.age("aaaaaaaaaaa.nfo", 5)
        os.utime(self.ch / "aaaaaaaaaaa.mp4", None)  # video re-embedded since
        _, out, _ = run(self.lib)
        self.assertIn("tvshow.nfo", out)
        self.assertTrue((self.ch / "poster.jpg").exists())


class CleanupTests(Base):
    def setUp(self):
        super().setUp()
        self.keep = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        gone = self.video("bbbbbbbbbbb", mp4_bytes(full_items()))
        run(self.lib)
        gone.unlink()

    def test_removes_only_orphans_written_by_the_tool(self):
        (self.ch / "abcdefghijk.nfo").write_text("<episodedetails><title>mine</title></episodedetails>")
        (self.ch / "notes.nfo").write_text("<x/>")
        (self.ch / "bbbbbbbbbbb.en.vtt").write_text("subs")
        code, out, _ = run(self.lib, "--cleanup")
        names = {p.name for p in self.ch.iterdir()}
        self.assertNotIn("bbbbbbbbbbb.nfo", names)
        self.assertNotIn("bbbbbbbbbbb-thumb.jpg", names)
        for survivor in ("aaaaaaaaaaa.nfo", "aaaaaaaaaaa-thumb.jpg", "tvshow.nfo", "poster.jpg",
                         "abcdefghijk.nfo", "notes.nfo", "bbbbbbbbbbb.en.vtt"):
            self.assertIn(survivor, names)

    def test_nothing_removed_without_flag(self):
        run(self.lib)
        self.assertTrue((self.ch / "bbbbbbbbbbb.nfo").exists())

    def test_dry_run_removes_nothing(self):
        run(self.lib, "--cleanup", "--dry-run")
        self.assertTrue((self.ch / "bbbbbbbbbbb.nfo").exists())

    def test_folder_with_no_videos_is_never_cleaned(self):
        self.keep.unlink()
        run(self.lib, "--cleanup")
        self.assertTrue((self.ch / "aaaaaaaaaaa.nfo").exists())


class EmptyLibraryTests(Base):
    def test_no_channel_folders_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "not-a-channel").mkdir()
            code, _, err = run(d)
        self.assertEqual(code, 1)
        self.assertIn("no channel folders", err)


class ViewBase(Base):
    def setUp(self):
        super().setUp()
        self._vtmp = tempfile.TemporaryDirectory()
        self.view = Path(self._vtmp.name)
        self.show = self.view / "NBC News"

    def tearDown(self):
        self._vtmp.cleanup()
        super().tearDown()

    def vrun(self, *args):
        return run(self.lib, "--view-dir", str(self.view), *args)

    def snapshot(self):
        return sorted((str(p.relative_to(self.lib)), p.stat().st_size, p.stat().st_mtime_ns)
                      for p in self.lib.rglob("*") if p.is_file())

    def episode_links(self):
        return sorted(self.show.glob("Season */*_[[]*[]]_*.mp4"))


class ViewModeTests(ViewBase):
    def test_builds_readable_linked_library_and_leaves_ta_untouched(self):
        mp4 = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        (self.ch / "aaaaaaaaaaa.en.vtt").write_text("subs")
        before = self.snapshot()
        code, _, err = self.vrun()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.snapshot(), before)
        (link,) = self.episode_links()
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path(os.readlink(link)), mp4.absolute())
        self.assertEqual(link.parent.name, "Season 2026")
        self.assertEqual(link.name, "20260911_[aaaaaaaaaaa]_TA title.mp4")
        base = link.name[:-4]
        for name in (f"{base}.nfo", f"{base}-thumb.jpg", f"{base}.en.vtt", "season.nfo"):
            self.assertTrue((link.parent / name).exists(), name)
        for name in ("tvshow.nfo", "poster.jpg", "banner.jpg", "fanart.jpg"):
            self.assertTrue((self.show / name).exists(), name)
        self.assertIn("<title>2026</title>", (link.parent / "season.nfo").read_text())

    def test_older_name_format_is_still_recognised(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.vrun()
        (link,) = self.episode_links()
        old = link.parent / "2026-09-11 - TA title [aaaaaaaaaaa]"
        for f in list(link.parent.iterdir()):
            if f.name.startswith("20260911_"):
                f.rename(link.parent / (str(old.name) + f.name[len("20260911_[aaaaaaaaaaa]_TA title"):]))
        code, out, _ = self.vrun()
        self.assertEqual(code, 0)
        self.assertIn("0 files written", out)
        self.assertEqual([p.name for p in self.episode_links()], [])  # old names do not match the new glob
        self.assertEqual(len(list(link.parent.glob("*[[]aaaaaaaaaaa[]].mp4"))), 1)
        self.vrun("--cleanup")
        self.assertEqual(len(list(link.parent.glob("*[[]aaaaaaaaaaa[]].mp4"))), 1)

    def test_long_titles_are_cut_in_the_file_name_but_not_in_the_nfo(self):
        long_title = "A very long title " * 8
        self.video("aaaaaaaaaaa", mp4_bytes(full_items(title=long_title)))
        self.vrun()
        (link,) = self.episode_links()
        title_part = link.name[len("20260911_[aaaaaaaaaaa]_"):-len(".mp4")]
        self.assertLessEqual(len(title_part), 64)
        self.assertTrue(long_title.strip().startswith(title_part))
        self.assertIn(long_title.strip(), (link.parent / (link.name[:-4] + ".nfo")).read_text())

    def test_rerun_is_a_no_op(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.vrun()
        code, out, _ = self.vrun()
        self.assertEqual(code, 0)
        self.assertIn("0 files written", out)

    def test_shorts_and_streams_get_offset_seasons(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items(vid_type="shorts")))
        self.video("bbbbbbbbbbb", mp4_bytes(full_items(vid_type="streams")))
        self.vrun()
        self.assertEqual(sorted(p.name for p in self.show.glob("Season *")), ["Season 12026", "Season 22026"])
        self.assertIn("2026 Shorts", (self.show / "Season 12026" / "season.nfo").read_text())

    def test_first_name_wins_when_title_or_channel_changes(self):
        path = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.vrun()
        first = [p.name for p in self.episode_links()]
        path.write_bytes(mp4_bytes(full_items(title="New title", channel={
            "channel_id": CHAN, "channel_name": "Renamed", "channel_description": "x"})))
        for nfo in self.show.glob("Season */20*.nfo"):
            os.utime(nfo, (time.time() - 100, time.time() - 100))  # older than the re-embedded video
        self.vrun()
        self.assertEqual([p.name for p in self.episode_links()], first)
        self.assertEqual([p.name for p in self.view.iterdir()], ["NBC News"])
        self.assertIn("New title", next(self.show.glob("Season */20*.nfo")).read_text())

    def test_channel_name_collision_gets_id_suffix(self):
        other = self.lib / ("UC" + "a" * 22)
        other.mkdir()
        (other / "ccccccccccc.mp4").write_bytes(mp4_bytes(full_items()))
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.vrun()
        names = sorted(p.name for p in self.view.iterdir())
        self.assertEqual(len(names), 2)
        self.assertIn("NBC News", names)
        self.assertTrue(any(n.startswith("NBC News [UC") for n in names))

    def test_an_empty_channel_folder_is_reused_not_doubled(self):
        (self.view / "NBC News").mkdir()  # left behind by a run that stopped before writing anything
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.vrun()
        self.assertEqual([p.name for p in self.view.iterdir()], ["NBC News"])
        self.assertTrue((self.show / "tvshow.nfo").exists())

    def test_a_run_stopped_partway_leaves_a_folder_the_next_run_reuses(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        with mock.patch.object(ta_nfo, "ensure_link", side_effect=KeyboardInterrupt):  # the stop, before any episode
            code, out, _ = self.vrun()
        self.assertEqual(code, 130)  # Ctrl-C stops cleanly with a summary instead of a traceback
        self.assertIn("interrupted", out)
        self.assertTrue((self.show / "tvshow.nfo").exists())
        self.vrun()
        self.assertEqual([p.name for p in self.view.iterdir()], ["NBC News"])
        self.assertEqual(len(self.episode_links()), 1)

    def test_tvshow_nfo_exists_before_any_episode_is_written(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        order = []
        real = ta_nfo.Ctx.write_xml
        def spy(ctx, path, root, ref):
            order.append(path.name)
            return real(ctx, path, root, ref)
        with mock.patch.object(ta_nfo.Ctx, "write_xml", spy):
            self.vrun()
        self.assertEqual(order[0], "tvshow.nfo")

    def test_a_folder_with_other_content_still_counts_as_taken(self):
        (self.view / "NBC News").mkdir()
        (self.view / "NBC News" / "mine.txt").write_text("x")
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.vrun()
        self.assertTrue(any(p.name.startswith("NBC News [UC") for p in self.view.iterdir()))

    def test_names_are_sanitised(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items(title='A/B: "C"? ' + "x" * 300 + "...")))
        self.vrun()
        (link,) = self.episode_links()
        self.assertTrue(set('/\\:*?"<>|').isdisjoint(link.name))
        self.assertLessEqual(len(link.name.encode()), 200)
        self.assertEqual(ta_nfo.clean_name("..  "), "untitled")
        self.assertEqual(ta_nfo.clean_name("é" * 200, 120), "é" * 60)
        self.assertEqual(ta_nfo.clean_name("ab " * 50, max_chars=10), "ab ab ab a")
        self.assertEqual(ta_nfo.clean_name("é" * 200, 120, max_chars=64), "é" * 60)  # the byte cap still applies

    def test_link_root_rewrites_symlink_targets(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.vrun("--link-root", "/data/ta")
        (link,) = self.episode_links()
        self.assertEqual(os.readlink(link), f"/data/ta/{CHAN}/aaaaaaaaaaa.mp4")

    def test_changed_link_root_repairs_existing_links(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.vrun()
        self.vrun("--link-root", "/data/ta")
        (link,) = self.episode_links()
        self.assertEqual(os.readlink(link), f"/data/ta/{CHAN}/aaaaaaaaaaa.mp4")

    def test_hard_links(self):
        mp4 = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        with tempfile.TemporaryDirectory(dir=self.lib.parent) as other_view:
            code, _, err = run(self.lib, "--view-dir", other_view, "--link-type", "hard")
            if os.stat(other_view).st_dev != os.stat(self.lib).st_dev:
                self.assertEqual(code, 1)
                self.assertIn("same filesystem", err)
                return
            self.assertEqual(code, 0, err)
            (link,) = Path(other_view).glob("*/Season */*_[[]*[]]_*.mp4")
            self.assertFalse(link.is_symlink())
            self.assertTrue(os.path.samefile(link, mp4))

    def test_option_conflicts_are_rejected(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        for args, text in ((["--in-place", "--link-type", "hard"], "cannot be used with --in-place"),
                           (["--in-place", "--view-dir", str(self.view)], "cannot be used with --in-place"),
                           (["--in-place", "--playlists"], "cannot be used with --in-place"),
                           (["--view-dir", str(self.lib / "view")], "outside the library"),
                           (["--view-dir", str(self.view), "--link-type", "hard", "--link-root", "/x"], "symlinks")):
            code, _, err = run(self.lib, *args)
            self.assertEqual(code, 1)
            self.assertIn(text, err)

    def test_dry_run_writes_nothing(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        code, out, _ = self.vrun("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("link ", out)
        self.assertEqual(list(self.view.iterdir()), [])

    def test_cleanup_removes_only_dead_view_entries(self):
        keep = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        gone = self.video("bbbbbbbbbbb", mp4_bytes(full_items(published=1700000000, title="Old")))
        (self.ch / "bbbbbbbbbbb.en.vtt").write_text("subs")
        self.vrun()
        (self.show / "Season 2026" / "notes.txt").write_text("mine")
        (self.show / "Season 2023" / "keep me [abcdefghijk].mp4").write_text("real file")
        gone.unlink()
        (self.ch / "bbbbbbbbbbb.en.vtt").unlink()
        code, _, _ = self.vrun("--cleanup")
        self.assertEqual(code, 0)
        self.assertEqual([p.name for p in self.episode_links() if "bbbbbbbbbbb" in p.name], [])
        self.assertFalse(list(self.show.glob("Season */*bbbbbbbbbbb*")))
        self.assertTrue(any("aaaaaaaaaaa" in p.name for p in self.episode_links()))
        self.assertTrue((self.show / "Season 2026" / "notes.txt").exists())
        self.assertTrue((self.show / "Season 2023" / "keep me [abcdefghijk].mp4").exists())
        self.assertTrue(keep.exists())

    def test_cleanup_removes_emptied_season_folder(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        gone = self.video("bbbbbbbbbbb", mp4_bytes(full_items(published=1700000000)))
        self.vrun()
        gone.unlink()
        self.vrun("--cleanup")
        self.assertFalse((self.show / "Season 2023").exists())
        self.assertTrue((self.show / "Season 2026").exists())

    def test_cleanup_skips_channel_with_no_videos(self):
        gone = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.vrun()
        gone.unlink()
        self.vrun("--cleanup")
        self.assertEqual(len(self.episode_links()), 1)

    def test_in_place_mode_needs_the_flag(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        run(self.lib, "--in-place")
        self.assertTrue((self.ch / "aaaaaaaaaaa.nfo").exists())

    def test_default_builds_the_view_in_the_script_folder(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        script_dir = self.lib.parent / (self.lib.name + "-script")
        script_dir.mkdir()
        self.addCleanup(shutil.rmtree, script_dir)
        with mock.patch.object(ta_nfo, "SCRIPT_DIR", script_dir):
            code, _, err = run(self.lib, auto_mode=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(list(script_dir.glob("*/Season */*_[[]*[]]_*.mp4"))), 1)
        self.assertFalse((self.ch / "aaaaaaaaaaa.nfo").exists())

    def test_default_view_rejects_a_symlinked_script(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        script_dir = self.lib.parent / (self.lib.name + "-script")
        script_dir.mkdir()
        self.addCleanup(shutil.rmtree, script_dir)
        link = script_dir / "ta_nfo.py"
        link.symlink_to(Path(ta_nfo.__file__).resolve())
        with mock.patch.object(ta_nfo, "SCRIPT_FILE", link), mock.patch.object(ta_nfo, "SCRIPT_DIR", script_dir):
            code, _, err = run(self.lib, auto_mode=False)
            self.assertEqual(code, 1)
            self.assertIn("is a symlink", err)
            self.assertEqual(sorted(p.name for p in script_dir.iterdir()), ["ta_nfo.py"])
            code, _, err = run(self.lib, "--view-dir", str(script_dir / "view"), auto_mode=False)
            self.assertEqual(code, 0, err)

    def test_a_view_that_cannot_hold_symlinks_fails_once_and_clearly(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        with mock.patch("os.symlink", side_effect=OSError(95, "Operation not supported")):
            code, out, err = self.vrun()
            self.assertEqual(code, 1)
            self.assertIn("cannot create symlinks", err)
            self.assertIn("Operation not supported", err)
            self.assertEqual(err.count("error:"), 1)  # one clear message, not one per file
            self.assertEqual(list(self.view.iterdir()), [])
            code, _, err = self.vrun("--link-type", "hard")  # hard links and --dry-run skip the check
            self.assertEqual(code, 0, err)

    def test_default_view_inside_the_library_is_rejected(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        with mock.patch.object(ta_nfo, "SCRIPT_DIR", self.lib):
            code, _, err = run(self.lib, auto_mode=False)
        self.assertEqual(code, 1)
        self.assertIn("script folder", err)
        self.assertEqual(sorted(p.name for p in self.lib.iterdir()), [CHAN])


def playlist(pid="TA_playlist_aaaaaaaa-1111", name="Mix", refresh=100, entries=()):
    return {"playlist_id": pid, "playlist_name": name, "playlist_last_refresh": refresh, "playlist_active": False,
            "playlist_entries": [{"youtube_id": v, "title": f"Title {v}", "idx": i, "downloaded": True}
                                 for i, v in enumerate(entries)]}


class PlaylistTests(ViewBase):
    """--playlists: one .m3u8 per TubeArchivist playlist, in the view."""

    def setUp(self):
        super().setUp()
        self.pdir = self.view / "Playlists"

    def two_videos(self, playlists, **kw):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items(playlists, **kw)))
        self.video("bbbbbbbbbbb", mp4_bytes(full_items(playlists, **kw)))

    def entries(self, path):
        lines = path.read_text(encoding="utf-8").splitlines()
        return [ln for ln in lines if not ln.startswith("#")]

    def test_off_by_default(self):
        self.two_videos([playlist(entries=["aaaaaaaaaaa", "bbbbbbbbbbb"])])
        self.vrun()
        self.assertFalse(self.pdir.exists())

    def test_writes_ordered_playlist_with_working_relative_paths(self):
        # the playlist lists b before a, plus a video that is not in the view
        self.two_videos([playlist(entries=["bbbbbbbbbbb", "zzzzzzzzzzz", "aaaaaaaaaaa"])])
        code, _, err = self.vrun("--playlists")
        self.assertEqual((code, err), (0, ""))
        (f,) = self.pdir.glob("*.m3u8")
        self.assertEqual(f.name, "Mix.m3u8")
        text = f.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#EXTM3U\n#PLAYLIST:Mix\n# ta_nfo.py playlist_id=TA_playlist_aaaaaaaa-1111 refreshed=100\n"))
        self.assertIn("#EXTINF:-1,Title bbbbbbbbbbb", text)
        rel = self.entries(f)
        self.assertEqual(len(rel), 2)  # the missing video is skipped
        self.assertIn("[bbbbbbbbbbb]", rel[0])
        self.assertIn("[aaaaaaaaaaa]", rel[1])
        for r in rel:
            self.assertTrue(r.startswith("../NBC News/Season 2026/"))
            self.assertTrue((self.pdir / r).is_symlink() and (self.pdir / r).exists())

    def test_first_run_on_an_existing_view_reads_everything_then_is_a_no_op(self):
        self.two_videos([playlist(entries=["aaaaaaaaaaa", "bbbbbbbbbbb"])])
        self.vrun()  # view built without --playlists
        self.assertFalse(self.pdir.exists())
        self.vrun("--playlists")
        self.assertEqual(len(list(self.pdir.glob("*.m3u8"))), 1)
        code, out, _ = self.vrun("--playlists")
        self.assertEqual(code, 0)
        self.assertIn("0 files written", out)

    def test_dry_run_writes_no_playlist(self):
        self.two_videos([playlist(entries=["aaaaaaaaaaa", "bbbbbbbbbbb"])])
        code, out, _ = self.vrun("--playlists", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Mix.m3u8", out)
        self.assertFalse(self.pdir.exists())

    def test_same_name_different_ids_and_first_name_wins(self):
        self.two_videos([playlist("TA_playlist_aaaaaaaa-1", "Mix", 100, ["aaaaaaaaaaa"]),
                         playlist("TA_playlist_bbbbbbbb-2", "Mix", 100, ["bbbbbbbbbbb"])])
        self.vrun("--playlists")
        names = sorted(p.name for p in self.pdir.glob("*.m3u8"))
        self.assertEqual(len(names), 2)
        self.assertIn("Mix.m3u8", names)
        self.assertTrue(any(n.startswith("Mix [") for n in names))
        # a renamed playlist keeps its file name
        self.video("ccccccccccc", mp4_bytes(full_items([playlist("TA_playlist_aaaaaaaa-1", "Renamed", 200,
                                                                  ["aaaaaaaaaaa", "ccccccccccc"])])))
        self.vrun("--playlists")
        self.assertEqual(sorted(p.name for p in self.pdir.glob("*.m3u8")), names)
        first = next(p for p in self.pdir.glob("*.m3u8") if "aaaaaaaa-1" in p.read_text())
        self.assertIn("#PLAYLIST:Renamed", first.read_text())
        self.assertEqual(len(self.entries(first)), 2)

    def test_an_older_snapshot_does_not_replace_a_newer_file(self):
        self.two_videos([playlist(refresh=100, entries=["aaaaaaaaaaa", "bbbbbbbbbbb"])])
        self.vrun("--playlists")
        (f,) = self.pdir.glob("*.m3u8")
        text = f.read_text().replace("refreshed=100", "refreshed=999")
        f.write_text(text)
        self.vrun("--playlists", "--overwrite")
        self.assertEqual(f.read_text(), text)

    def test_cleanup_drops_dead_entries_and_removes_empty_playlists_but_not_foreign_files(self):
        self.two_videos([playlist(entries=["aaaaaaaaaaa", "bbbbbbbbbbb"])])
        self.video("ccccccccccc", mp4_bytes(full_items()))  # keeps the channel non-empty, so cleanup runs
        self.vrun("--playlists")
        foreign = self.pdir / "mine.m3u8"
        foreign.write_text("#EXTM3U\n#EXTINF:-1,x\n../gone.mp4\n")
        (f,) = [p for p in self.pdir.glob("*.m3u8") if p != foreign]
        (self.ch / "aaaaaaaaaaa.mp4").unlink()
        self.vrun("--playlists", "--cleanup")
        self.assertEqual(len(self.entries(f)), 1)
        self.assertIn("[bbbbbbbbbbb]", self.entries(f)[0])
        self.assertTrue(foreign.exists())
        (self.ch / "bbbbbbbbbbb.mp4").unlink()
        self.vrun("--playlists", "--cleanup")
        self.assertFalse(f.exists())
        self.assertTrue(foreign.exists())

    def test_a_channel_called_playlists_does_not_collide_with_the_folder(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items([playlist(entries=["aaaaaaaaaaa"])], channel={
            "channel_id": CHAN, "channel_name": "Playlists", "channel_description": "x"})) )
        self.vrun("--playlists")
        self.assertTrue((self.pdir / "Mix.m3u8").exists())
        self.assertFalse((self.pdir / "tvshow.nfo").exists())
        self.assertTrue(any(p.name.startswith("Playlists [") for p in self.view.iterdir()))


class UnreadableVideoTests(Base):
    """A video that cannot be read is reported once, with details; a hiccup on the share is retried first."""

    def test_a_read_that_fails_once_is_retried_and_succeeds(self):
        video = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        real = ta_nfo.read
        calls = []

        def flaky(path, images=False, thumb=True):
            calls.append(path)
            if len(calls) == 1:
                raise ValueError("not a readable MP4 (a short read from the share)")
            return real(path, images, thumb)
        with mock.patch.object(ta_nfo, "read", flaky):
            _, meta, err = ta_nfo.safe_read(video)
        self.assertIsNone(err)
        self.assertEqual(meta["title"], "TA title")
        self.assertEqual(len(calls), 2)

    def test_a_read_that_keeps_failing_is_tried_twice_and_reported(self):
        video = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        with mock.patch.object(ta_nfo, "read", side_effect=OSError(5, "Input/output error")) as read:
            _, meta, err = ta_nfo.safe_read(video)
        self.assertIsNone(meta)
        self.assertIsInstance(err, OSError)
        self.assertEqual(read.call_count, 2)

    def test_a_bug_is_not_retried(self):
        video = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        with mock.patch.object(ta_nfo, "read", side_effect=KeyError("x")) as read:
            _, meta, err = ta_nfo.safe_read(video)
        self.assertIsInstance(err, KeyError)
        self.assertEqual(read.call_count, 1)

    def test_the_message_says_what_was_found(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items())[:60], age_days=3)  # cut off: no moov box
        self.video("bbbbbbbbbbb", b"")
        _, _, trunc = ta_nfo.safe_read(self.ch / "aaaaaaaaaaa.mp4")
        _, _, empty = ta_nfo.safe_read(self.ch / "bbbbbbbbbbb.mp4")
        self.assertIn("60 bytes", str(trunc))
        self.assertIn("last modified 72h00m ago", str(trunc))
        self.assertIn("the file is empty (0 bytes)", str(empty))

    def test_a_video_that_cannot_be_opened_at_all_is_still_an_error(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.video("bbbbbbbbbbb", mp4_bytes(full_items()))
        real = ta_nfo.read

        def denied(path, images=False, thumb=True):
            if Path(path).stem == "bbbbbbbbbbb":
                raise PermissionError(13, "Permission denied")
            return real(path, images, thumb)
        with mock.patch.object(ta_nfo, "read", denied):
            code, _, err = run(self.lib, "--in-place")
        self.assertEqual(code, 1)  # permissions or a sick share are a real problem
        self.assertIn("error: cannot read", err)
        self.assertTrue((self.ch / "aaaaaaaaaaa.nfo").exists())

    def test_too_many_unreadable_videos_is_an_error(self):
        for i in range(12):  # 12 of 14: this is not a few broken files, it is the wrong folder or a bad share
            self.video(f"bad{i:08d}", b"not an mp4")
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.video("bbbbbbbbbbb", mp4_bytes(full_items()))
        code, out, err = run(self.lib, "--in-place")
        self.assertEqual(code, 1)
        self.assertIn("too many to be a few broken files", err)
        self.assertIn("12 unreadable", out)

    def test_a_few_unreadable_videos_among_many_stay_a_warning(self):
        for i in range(12):
            self.video(f"bad{i:08d}", b"not an mp4")
        for i in range(400):  # 12 of 412 is under 5 percent
            self.video(f"ok{i:09d}", mp4_bytes(full_items()))
        code, out, err = run(self.lib, "--in-place", "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("12 unreadable", out)
        self.assertIn("... and 2 more", out)  # the recap lists ten and counts the rest

    def test_one_bad_video_is_one_warning_and_the_rest_still_get_their_files(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        self.video("bbbbbbbbbbb", mp4_bytes(full_items())[:60])
        code, out, err = run(self.lib, "--in-place")
        self.assertEqual(code, 0)
        self.assertEqual(err.count("warning:"), 1)
        self.assertNotIn("error:", err)
        self.assertIn("bbbbbbbbbbb.mp4", err)
        self.assertTrue((self.ch / "aaaaaaaaaaa.nfo").exists())
        self.assertFalse((self.ch / "bbbbbbbbbbb.nfo").exists())


class UnreadableNewestVideoTests(ViewBase):
    """The newest video gives the series info; when it is unreadable the channel must still be built."""

    def setup_videos(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()), age_days=1)
        self.video("bbbbbbbbbbb", mp4_bytes(full_items())[:60])  # the newest, and cut off

    def test_a_new_channel_is_built_from_the_next_readable_video(self):
        self.setup_videos()
        code, _, err = self.vrun()
        self.assertEqual(code, 0)
        self.assertEqual(err.count("warning:"), 1)  # reported once, not once per step that touched it
        self.assertIn("bbbbbbbbbbb.mp4", err)
        self.assertTrue((self.show / "tvshow.nfo").exists())
        for name in ("poster.jpg", "banner.jpg", "fanart.jpg"):
            self.assertTrue((self.show / name).exists(), name)
        names = [p.name for p in self.episode_links()]
        self.assertEqual(len(names), 1)
        self.assertIn("[aaaaaaaaaaa]", names[0])

    def test_once_the_file_is_fixed_the_next_run_adds_it_without_a_duplicate_channel(self):
        self.setup_videos()
        self.vrun()
        self.video("bbbbbbbbbbb", mp4_bytes(full_items()))
        code, _, err = self.vrun()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([p.name for p in self.view.iterdir()], ["NBC News"])
        self.assertEqual(len(self.episode_links()), 2)

    def test_in_place_the_channel_info_comes_from_the_next_readable_video(self):
        self.setup_videos()
        code, _, err = run(self.lib, "--in-place")
        self.assertEqual(code, 0)
        self.assertEqual(err.count("warning:"), 1)
        self.assertTrue((self.ch / "tvshow.nfo").exists())
        self.assertTrue((self.ch / "poster.jpg").exists())
        self.assertTrue((self.ch / "aaaaaaaaaaa.nfo").exists())
        self.assertFalse((self.ch / "bbbbbbbbbbb.nfo").exists())

    def test_every_video_unreadable_gives_one_warning_each_and_no_channel_info(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items())[:60], age_days=1)
        self.video("bbbbbbbbbbb", mp4_bytes(full_items())[:60])
        code, _, err = self.vrun()
        self.assertEqual(code, 0)  # only two: a few broken files, not a systemic problem
        self.assertEqual(err.count("warning:"), 2)
        self.assertEqual(list(self.view.iterdir()), [])


class SpeedAndResumeTests(ViewBase):
    """The read pipeline, parallel stats, --progress, and resuming an interrupted first --playlists scan."""

    OTHER = "UC" + "z" * 22  # sorts after CHAN, so it is the second channel processed

    def two_channels(self):
        both = [playlist(entries=["aaaaaaaaaaa", "bbbbbbbbbbb"])]
        self.video("aaaaaaaaaaa", mp4_bytes(full_items(both)))
        other = self.lib / self.OTHER
        other.mkdir()
        (other / "bbbbbbbbbbb.mp4").write_bytes(mp4_bytes(full_items(both)))

    def read_counts(self):
        """Context manager result: {channel folder name: number of tag reads} while it is active."""
        counts = collections.Counter()
        real = ta_nfo.read_tags

        def spy(path, skip=()):
            counts[Path(path).parent.name] += 1
            return real(path, skip=skip)
        return counts, mock.patch.object(ta_nfo, "read_tags", spy)

    def test_prefetch_keeps_order_and_stays_within_the_window(self):
        running, peak = 0, 0
        lock = threading.Lock()

        def work(n):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.002)
            with lock:
                running -= 1
            return n * 2

        started = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            def tracked(n):
                started.append(n)
                return work(n)
            consumed = []
            for result in ta_nfo.prefetch(pool, tracked, range(50), 5):
                consumed.append(result)
                # never more than the window ahead of what was consumed (plus the one just handed over)
                self.assertLessEqual(len(started) - len(consumed), 5)
        self.assertEqual(consumed, [n * 2 for n in range(50)])
        self.assertLessEqual(peak, 5)

    def test_prefetch_stops_cleanly_when_the_consumer_gives_up(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            gen = ta_nfo.prefetch(pool, lambda n: n, range(1000), 4)
            self.assertEqual(next(gen), 0)
            gen.close()  # the cancel in its finally must not raise or hang

    def test_scan_videos_gives_the_same_answer_with_and_without_a_pool(self):
        for i in range(80):
            self.video(f"v{i:010d}", b"x", age_days=i % 7)
        (self.ch / "notavideo.txt").write_text("x")
        plain = ta_nfo.scan_videos(self.ch)
        with ThreadPoolExecutor(max_workers=8) as pool:
            parallel = ta_nfo.scan_videos(self.ch, pool)
        self.assertEqual(len(plain), 80)
        self.assertEqual(plain, parallel)

    def test_default_worker_count_is_higher_than_the_old_four(self):
        self.assertGreaterEqual(ta_nfo.DEFAULT_WORKERS, 16)

    def test_reading_still_works_where_posix_fadvise_is_missing_or_fails(self):
        video = self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        with mock.patch.object(os, "posix_fadvise", side_effect=OSError("no")):
            self.assertIn("ta", ta_nfo.read_tags(video))
        with mock.patch.object(os, "posix_fadvise", side_effect=AttributeError):
            self.assertIn("ta", ta_nfo.read_tags(video))

    def test_progress_lines_and_time_breakdown(self):
        self.two_channels()
        code, out, err = self.vrun("--progress", "--quiet")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2 channels, 2 videos to look at", out)
        self.assertIn("[1/2]", out)
        self.assertIn("[2/2]", out)
        self.assertRegex(out, r"time: \d+s in total = .* listing .* waiting for tag reads .* everything else")
        self.assertIn("2 videos read", out)

    def test_duration_text(self):
        self.assertEqual(ta_nfo.fmt_duration(7), "7s")
        self.assertEqual(ta_nfo.fmt_duration(61), "1m01s")
        self.assertEqual(ta_nfo.fmt_duration(3661), "1h01m")
        self.assertEqual(ta_nfo.fmt_duration(20 * 3600), "20h00m")

    def interrupt_second_channel(self):
        real = ta_nfo.process_channel_view
        calls = []

        def flaky(folder, ctx, pool, vindex):
            calls.append(folder.name)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(folder, ctx, pool, vindex)
        return mock.patch.object(ta_nfo, "process_channel_view", flaky)

    def test_an_interrupted_first_playlist_scan_resumes_instead_of_starting_over(self):
        self.two_channels()
        pdir = self.view / "Playlists"
        with self.interrupt_second_channel():
            code, out, _ = self.vrun("--playlists")
        self.assertEqual(code, 130)
        self.assertIn("interrupted", out)
        # channel 1 is finished and remembered; its playlist file was written before the stop
        self.assertEqual((pdir / ".scanned").read_text().split(), [CHAN])
        (mix,) = pdir.glob("*.m3u8")
        self.assertEqual(len(ListEntries.of(mix)), 1)
        # the second run reads channel 2 in full, and channel 1 only for its newest video (the series info)
        counts, patch = self.read_counts()
        with patch:
            code, _, err = self.vrun("--playlists")
        self.assertEqual((code, err), (0, ""))
        # a new channel's newest video is opened once: series info, artwork and episode metadata in one read
        self.assertEqual(counts[self.OTHER], 1)
        self.assertEqual(counts[CHAN], 1)        # only the newest video: nothing is read again
        self.assertEqual(sorted((pdir / ".scanned").read_text().split()), sorted([CHAN, self.OTHER]))
        self.assertEqual(len(ListEntries.of(mix)), 2)  # both videos, now that channel 2 has been read

    def test_a_playlist_only_re_read_skips_the_thumbnail_and_changes_no_file(self):
        both = [playlist(entries=["aaaaaaaaaaa", "bbbbbbbbbbb"])]
        self.video("aaaaaaaaaaa", mp4_bytes(full_items(both)))
        self.video("bbbbbbbbbbb", mp4_bytes(full_items(both)), age_days=1)  # older, so not the newest video
        self.vrun()  # a view built without --playlists: both videos are finished
        before = sorted((str(p.relative_to(self.view)), p.stat().st_mtime_ns)
                        for p in self.view.rglob("*") if p.is_file() and not p.is_symlink())
        skips = {}
        real = ta_nfo.read_tags

        def spy(path, skip=()):
            skips[Path(path).stem] = set(skip)
            return real(path, skip=skip)
        with mock.patch.object(ta_nfo, "read_tags", spy):
            code, _, err = self.vrun("--playlists")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("covr", skips["aaaaaaaaaaa"])  # the newest video is read in full for the series info
        self.assertIn("covr", skips["bbbbbbbbbbb"])     # a finished video is only read for its playlists
        after = sorted((str(p.relative_to(self.view)), p.stat().st_mtime_ns)
                       for p in self.view.rglob("*") if p.is_file() and not p.is_symlink()
                       if "Playlists" not in p.parts)
        self.assertEqual([x for x in before if "Playlists" not in x[0]], after)  # no episode file was rewritten
        (mix,) = (self.view / "Playlists").glob("*.m3u8")
        self.assertEqual(len(ListEntries.of(mix)), 2)

    def test_a_finished_scan_reads_nothing_more_on_the_next_run(self):
        self.two_channels()
        self.vrun("--playlists")
        counts, patch = self.read_counts()
        with patch:
            code, out, _ = self.vrun("--playlists")
        self.assertEqual(code, 0)
        self.assertIn("0 files written", out)
        self.assertEqual(sum(counts.values()), 2)  # just each channel's newest video

    def test_a_playlists_folder_from_an_older_version_counts_as_a_finished_scan(self):
        self.two_channels()
        self.vrun()  # a view built without playlists
        (self.view / "Playlists").mkdir()  # what 0.3.0 to 0.4.2 left behind after a full scan
        counts, patch = self.read_counts()
        with patch:
            self.vrun("--playlists")
        self.assertFalse((self.view / "Playlists" / ".scanned").exists())
        self.assertEqual(sum(counts.values()), 2)  # no full re-read


class ListEntries:
    @staticmethod
    def of(path):
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.startswith("#")]


class MemoryTests(ViewBase):
    """A channel's memory use must not grow with its number of videos (each video embeds ~0.9 MB of images)."""

    BIG = b"\xff\xd8\xff" + bytes(300_000)

    def big_video(self, vid, with_images=True):
        items = [text_atom("©nam", "t"), text_atom("©ART", "a"), box(b"covr", data_box(JPEG, 13)),
                 freeform("ta", ta_json())]
        if with_images:
            items += [freeform(n, self.BIG, 13) for n in ("channel_icon", "channel_banner", "channel_tv")]
        return self.video(vid, mp4_bytes(items))

    def peak_mb(self, *args):
        tracemalloc.start()
        try:
            code, _, err = run(self.lib, *args)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual((code, err), (0, ""))
        return peak / 1e6

    def test_view_mode_memory_does_not_grow_with_the_channel(self):
        for i in range(40):  # about 36 MB of embedded images in total; holding them all would peak above that
            self.big_video(f"v{i:010d}")
        self.assertLess(self.peak_mb("--view-dir", str(self.view)), 12)
        self.assertEqual(len(self.episode_links()), 40)

    def test_in_place_memory_does_not_grow_with_the_channel(self):
        for i in range(40):
            self.big_video(f"v{i:010d}")
        self.assertLess(self.peak_mb("--in-place"), 12)
        self.assertEqual(len(list(self.ch.glob("*.nfo"))), 41)  # 40 episodes plus tvshow.nfo

    def test_a_no_op_rerun_does_not_read_the_channel_images(self):
        for i in range(3):
            self.big_video(f"v{i:010d}")
        self.vrun()
        with mock.patch.object(ta_nfo, "read_tags", wraps=ta_nfo.read_tags) as spy:
            code, out, _ = self.vrun()
        self.assertEqual(code, 0)
        self.assertIn("0 files written", out)
        self.assertTrue(spy.call_args_list)
        for call in spy.call_args_list:
            self.assertTrue(set(ta_nfo.CHANNEL_IMAGES) <= set(call.kwargs["skip"]), call)

    def test_channel_artwork_comes_from_an_older_video_when_the_newest_has_none(self):
        self.big_video("aaaaaaaaaaa", with_images=True)
        self.age("aaaaaaaaaaa.mp4", 5)
        self.big_video("bbbbbbbbbbb", with_images=False)  # newest, no channel images
        code, _, err = self.vrun()
        self.assertEqual((code, err), (0, ""))
        for name in ("poster.jpg", "banner.jpg", "fanart.jpg"):
            self.assertEqual((self.show / name).stat().st_size, len(self.BIG), name)


class VersionTests(unittest.TestCase):
    def test_version_flag_prints_the_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            ta_nfo.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue().split()[-1], ta_nfo.__version__)
        self.assertRegex(ta_nfo.__version__, r"^\d+\.\d+\.\d+$")

    def test_version_matches_the_latest_released_changelog_heading(self):
        changelog = (Path(__file__).parent / "CHANGELOG.md").read_text(encoding="utf-8")
        latest = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)[1]
        self.assertEqual(ta_nfo.__version__, latest)


class JellyfinRefreshTests(Base):
    def serve(self):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization"), self.headers.get("X-Emby-Token")))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, seen

    def test_refresh_called_after_changes_only(self):
        srv, seen = self.serve()
        url = f"http://127.0.0.1:{srv.server_port}"
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()), age_days=2)
        code, _, _ = run(self.lib, "--jellyfin-url", url, "--jellyfin-api-key", "KEY")
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("/Library/Refresh", 'MediaBrowser Token="KEY"', "KEY")])
        run(self.lib, "--jellyfin-url", url, "--jellyfin-api-key", "KEY")  # nothing changed
        self.assertEqual(len(seen), 1)

    def test_unreachable_jellyfin_gives_nonzero_exit(self):
        self.video("aaaaaaaaaaa", mp4_bytes(full_items()))
        code, _, err = run(self.lib, "--jellyfin-url", "http://127.0.0.1:1", "--jellyfin-api-key", "K")
        self.assertEqual(code, 1)
        self.assertIn("Jellyfin", err)


if __name__ == "__main__":
    unittest.main()
