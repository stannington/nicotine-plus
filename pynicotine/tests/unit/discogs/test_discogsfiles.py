# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import shutil
import struct

from unittest import TestCase
from unittest import skipUnless
from unittest.mock import patch

from pynicotine import discogsfiles
from pynicotine.config import config
from pynicotine.core import core
from pynicotine.discogsfiles import AudioFile
from pynicotine.discogsfiles import DiscogsTrack

CURRENT_FOLDER_PATH = os.path.dirname(os.path.realpath(__file__))
DATA_FOLDER_PATH = os.path.join(CURRENT_FOLDER_PATH, "temp_data_files")
COVER = (b"\xff\xd8\xff\xe0 not really a jpeg", "image/jpeg")


def artist(name, join=""):
    return {"name": name, "anv": "", "join": join}


RELEASE_DATA = {
    "id": 38528466,
    "title": "All My Life",
    "year": 2026,
    "artists": [artist("HLZ")],
    "labels": [{"name": "Metalheadz", "catno": "METALP031"}],
    "genres": ["Electronic"],
    "styles": ["Drum n Bass", "Jungle"],
    "tracklist": [
        {"position": "", "type_": "heading", "title": "Side A"},
        {"position": "1", "type_": "track", "title": "Eagle Dub", "artists": []},
        {"position": "2", "type_": "track", "title": "All My Life"},
        {"position": "3", "type_": "track", "title": "The Genie",
         "artists": [artist("HLZ", join=","), artist("Cleveland Watkiss (2)")]}
    ],
    "images": [{"type": "secondary", "uri": "https://example.com/back.jpg"},
               {"type": "primary", "uri": "https://example.com/front.jpg"}]
}


def write_flac(file_path):
    """Write a FLAC file with only the stream info block, enough for
    mutagen to read and tag it."""

    # Block sizes, frame sizes, then sample rate 44100 Hz, 2 channels, 16 bits and no samples
    stream_info = struct.pack(">HH3s3s", 4096, 4096, b"\0\0\0", b"\0\0\0")
    stream_info += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)

    with open(file_path, "wb") as file_handle:
        file_handle.write(b"fLaC" + bytes((0x80,)) + len(stream_info).to_bytes(3, "big") + stream_info)


def write_mp3(file_path):
    """Write a few silent MPEG frames (128 kbps, 44100 Hz)."""

    frame = b"\xff\xfb\x90\x00" + bytes(413)

    with open(file_path, "wb") as file_handle:
        file_handle.write(frame * 10)


class DiscogsFilesTest(TestCase):

    def test_get_artist_name(self):

        self.assertEqual(discogsfiles.get_artist_name([artist("Seba", "&"), artist("Paradox")]), "Seba & Paradox")
        self.assertEqual(discogsfiles.get_artist_name([artist("HLZ", ","), artist("Watkiss (2)")]), "HLZ, Watkiss")
        self.assertEqual(discogsfiles.get_artist_name([{"name": "Goldie", "anv": "Rufige Kru*"}]), "Rufige Kru")
        self.assertEqual(discogsfiles.get_artist_name([]), "")

    def test_get_tracks(self):

        tracks = discogsfiles.get_tracks(RELEASE_DATA)

        # Headings are skipped, and tracks without their own artists use the release artist
        self.assertEqual([(track.number, track.artist, track.title) for track in tracks], [
            (1, "HLZ", "Eagle Dub"),
            (2, "HLZ", "All My Life"),
            (3, "HLZ, Cleveland Watkiss", "The Genie")
        ])

    def test_get_cover_url(self):

        self.assertEqual(discogsfiles.get_cover_url(RELEASE_DATA), "https://example.com/front.jpg")
        self.assertEqual(discogsfiles.get_cover_url({"images": [{"uri": "https://example.com/back.jpg"}]}),
                         "https://example.com/back.jpg")
        self.assertIsNone(discogsfiles.get_cover_url({}))

    def test_get_file_name(self):

        track = DiscogsTrack(3, "HLZ, Cleveland Watkiss", "The Genie: Part 2")

        self.assertEqual(discogsfiles.get_file_name("$track $artist - $title", track, 16, ".flac"),
                         "03 HLZ, Cleveland Watkiss - The Genie_ Part 2.flac")
        self.assertEqual(discogsfiles.get_file_name("$track $title", track, 120, ".mp3"), "003 The Genie_ Part 2.mp3")

    def test_match_files_in_order(self):

        tracks = [DiscogsTrack(1, "A", "Intro"), DiscogsTrack(2, "A", "Second"), DiscogsTrack(10, "A", "Tenth")]

        # Natural sort order, so "10" comes after "2"
        files = [AudioFile("10.flac"), AudioFile("1.flac"), AudioFile("2.flac")]
        matches = discogsfiles.match_files(files, tracks)

        self.assertEqual({audio_file.name: track.title for audio_file, track in matches.items()},
                         {"1.flac": "Intro", "2.flac": "Second", "10.flac": "Tenth"})

        # Track number tags are used when the names have no order
        files = [AudioFile("x.flac", track_number=3), AudioFile("y.flac", track_number=1),
                 AudioFile("z.flac", track_number=2)]
        matches = discogsfiles.match_files(files, tracks)

        self.assertEqual({audio_file.name: track.title for audio_file, track in matches.items()},
                         {"y.flac": "Intro", "z.flac": "Second", "x.flac": "Tenth"})

    def test_match_files_by_title(self):

        tracks = [
            DiscogsTrack(1, "A", "Feet Don't Fail Me Now"),
            DiscogsTrack(2, "A", "Feet Don't Fail Me Now (Dub Mix)"),
            DiscogsTrack(3, "A", "Café Del Mar")
        ]

        # Same number of files, but the order disagrees with the titles
        files = [AudioFile("a - feet dont fail me now (dub mix).flac"), AudioFile("b - cafe del mar.flac"),
                 AudioFile("c - Feet Don't Fail Me Now.flac")]
        matches = discogsfiles.match_files(files, tracks)

        self.assertEqual({audio_file.name: track.number for audio_file, track in matches.items()}, {
            "a - feet dont fail me now (dub mix).flac": 2,
            "b - cafe del mar.flac": 3,
            "c - Feet Don't Fail Me Now.flac": 1
        })

        # Fewer files than tracks: unknown files are left out, as are tracks matched by two files
        files = [AudioFile("01 cafe del mar.flac"), AudioFile("01 cafe del mar (copy).flac"),
                 AudioFile("02 feet dont fail me now.mp3"), AudioFile("bonus.mp3", title_tag="Something Else")]
        matches = discogsfiles.match_files(files, tracks)

        self.assertEqual({audio_file.name: track.number for audio_file, track in matches.items()},
                         {"02 feet dont fail me now.mp3": 1})


@skipUnless(discogsfiles.HAS_MUTAGEN, "mutagen is not installed")
class ProcessFolderTest(TestCase):

    def setUp(self):
        self.folder_path = os.path.join(DATA_FOLDER_PATH, "HLZ - All My Life (WEB)")
        os.makedirs(self.folder_path)

    def tearDown(self):
        shutil.rmtree(DATA_FOLDER_PATH, ignore_errors=True)

    def test_process_folder(self):

        from mutagen.flac import FLAC
        from mutagen.id3 import ID3

        write_flac(os.path.join(self.folder_path, "01-hlz-eagle_dub.flac"))
        write_mp3(os.path.join(self.folder_path, "02-hlz-all_my_life.mp3"))
        write_flac(os.path.join(self.folder_path, "03-hlz-the_genie.flac"))

        result = discogsfiles.process_folder(
            self.folder_path, RELEASE_DATA, COVER, "$track $artist - $title", should_tag=True)

        self.assertEqual((result.num_files, result.num_tagged, result.num_renamed), (3, 3, 3))
        self.assertEqual((result.unmatched_file_names, result.errors), ([], []))
        self.assertTrue(result.saved_cover)
        self.assertEqual(sorted(os.listdir(self.folder_path)), [
            "01 HLZ - Eagle Dub.flac",
            "02 HLZ - All My Life.mp3",
            "03 HLZ, Cleveland Watkiss - The Genie.flac",
            "cover.jpg"
        ])

        flac = FLAC(os.path.join(self.folder_path, "03 HLZ, Cleveland Watkiss - The Genie.flac"))

        for key, values in (
            ("artist", ["HLZ, Cleveland Watkiss"]), ("albumartist", ["HLZ"]), ("title", ["The Genie"]),
            ("album", ["All My Life"]), ("tracknumber", ["3"]), ("tracktotal", ["3"]), ("date", ["2026"]),
            ("label", ["Metalheadz"]), ("catalognumber", ["METALP031"]), ("genre", ["Electronic"]),
            ("style", ["Drum n Bass", "Jungle"]), ("discogs_release_id", ["38528466"])
        ):
            with self.subTest(key=key):
                self.assertEqual(flac[key], values)

        self.assertEqual(flac.pictures[0].data, COVER[0])
        self.assertEqual(flac.pictures[0].type, 3)

        id3 = ID3(os.path.join(self.folder_path, "02 HLZ - All My Life.mp3"))

        self.assertEqual(id3.version[:2], (2, 3))
        self.assertEqual(str(id3["TPE1"]), "HLZ")
        self.assertEqual(str(id3["TIT2"]), "All My Life")
        self.assertEqual(str(id3["TRCK"]), "2/3")
        self.assertEqual(str(id3["TPUB"]), "Metalheadz")
        self.assertEqual(str(id3["TXXX:CATALOGNUMBER"]), "METALP031")
        # ID3v2.3 has no multiple values, so they are joined
        self.assertEqual(id3["TXXX:STYLE"].text, ["Drum n Bass; Jungle"])
        self.assertEqual(id3["APIC:Cover"].data, COVER[0])

        with open(os.path.join(self.folder_path, "cover.jpg"), "rb") as file_handle:
            self.assertEqual(file_handle.read(), COVER[0])

    def test_overwrite_tags_keep_cover_file(self):

        from mutagen.flac import FLAC

        file_path = os.path.join(self.folder_path, "track.flac")
        write_flac(file_path)

        flac = FLAC(file_path)
        flac["title"] = "Eagle Dub"
        flac["genre"] = "Wrong Genre"
        flac["comment"] = "Kept"
        flac.save()

        with open(os.path.join(self.folder_path, "Folder.JPG"), "wb") as file_handle:
            file_handle.write(b"existing")

        # Matched by the title tag. Fields Discogs has no value for are removed.
        release_data = dict(RELEASE_DATA, genres=[])
        result = discogsfiles.process_folder(self.folder_path, release_data, COVER, "", should_tag=True)

        self.assertEqual((result.num_tagged, result.num_renamed, result.saved_cover), (1, 0, False))
        self.assertFalse(os.path.exists(os.path.join(self.folder_path, "cover.jpg")))

        flac = FLAC(file_path)

        self.assertEqual(flac["title"], ["Eagle Dub"])
        self.assertEqual(flac["tracknumber"], ["1"])
        self.assertNotIn("genre", flac)
        self.assertEqual(flac["comment"], ["Kept"])

    def test_unmatched_files(self):

        write_flac(os.path.join(self.folder_path, "eagle dub.flac"))
        write_flac(os.path.join(self.folder_path, "unknown.flac"))

        with open(os.path.join(self.folder_path, "notes.txt"), "w", encoding="utf-8"):
            pass

        result = discogsfiles.process_folder(
            self.folder_path, RELEASE_DATA, None, "$track $artist - $title", should_tag=False)

        self.assertEqual((result.num_files, result.num_tagged, result.num_renamed), (2, 0, 1))
        self.assertEqual(result.unmatched_file_names, ["unknown.flac"])
        self.assertFalse(result.saved_cover)
        self.assertEqual(sorted(os.listdir(self.folder_path)), ["01 HLZ - Eagle Dub.flac", "notes.txt", "unknown.flac"])


class DownloadedReleaseTest(TestCase):

    # pylint: disable=protected-access

    def setUp(self):

        config.set_data_folder(DATA_FOLDER_PATH)
        config.set_config_file(os.path.join(DATA_FOLDER_PATH, "temp_config"))

        core.init_components(enabled_components={"discogs"})
        core.start()

        self.download_path = os.path.join(DATA_FOLDER_PATH, "downloads")
        config.sections["transfers"]["downloaddir"] = self.download_path
        config.sections["discogs"]["folder_name_pattern"] = "$year $catno $artist - $title"
        config.sections["discogs"]["file_name_pattern"] = "$track $artist - $title"
        config.sections["discogs"]["tag_files"] = True
        config.sections["discogs"]["pending_releases"] = []

    def tearDown(self):
        core.quit()
        shutil.rmtree(DATA_FOLDER_PATH, ignore_errors=True)

    def test_process_files_after_renaming_folder(self):

        core.discogs.add_pending_release("HLZ", "All My Life", 2026, "METALP031", "", 38528466)

        folder_path = os.path.join(self.download_path, "HLZ - All My Life")
        os.makedirs(folder_path)

        with patch("pynicotine.discogs.Discogs._start_thread") as mock_start_thread:
            core.discogs._folder_download_finished(folder_path)

        new_folder_path = os.path.join(self.download_path, "2026 METALP031 HLZ - All My Life")

        self.assertTrue(os.path.isdir(new_folder_path))
        self.assertEqual(config.sections["discogs"]["pending_releases"], [])
        self.assertEqual(
            mock_start_thread.call_args.args[1:],
            (new_folder_path, 38528466, "$track $artist - $title", True, config.sections["discogs"]["token"]))

    def test_process_files_without_renaming_folder(self):

        config.sections["discogs"]["folder_name_pattern"] = ""
        core.discogs.add_pending_release("HLZ", "All My Life", 2026, "METALP031", "", 38528466)

        folder_path = os.path.join(self.download_path, "HLZ - All My Life")
        os.makedirs(folder_path)

        with patch("pynicotine.discogs.Discogs._start_thread") as mock_start_thread:
            core.discogs._folder_download_finished(folder_path)

        self.assertTrue(os.path.isdir(folder_path))
        self.assertEqual(mock_start_thread.call_args.args[1], folder_path)

    @patch("pynicotine.discogs.Discogs._download_image", return_value=COVER)
    @patch("pynicotine.discogs.Discogs._request", return_value=RELEASE_DATA)
    def test_process_downloaded_files(self, mock_request, mock_download_image):

        with patch("pynicotine.discogsfiles.process_folder") as mock_process_folder, \
                patch("pynicotine.events.Events.invoke_main_thread") as mock_invoke:
            core.discogs._process_downloaded_files("folder", 38528466, "$track", True, "token")

        self.assertEqual(mock_request.call_args.args, ("/releases/38528466", {}, "token"))
        mock_download_image.assert_called_once_with("https://example.com/front.jpg")
        self.assertEqual(mock_process_folder.call_args.args, ("folder", RELEASE_DATA, COVER, "$track", True))

        _callback, folder_path, should_tag, result, error_message = mock_invoke.call_args.args

        self.assertEqual((folder_path, should_tag, error_message), ("folder", True, None))
        self.assertIs(result, mock_process_folder.return_value)
