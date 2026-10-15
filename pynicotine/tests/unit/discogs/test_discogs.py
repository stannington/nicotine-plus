# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import io
import os
import shutil

from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.parse import urlsplit

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.discogs import LibraryIndex

CURRENT_FOLDER_PATH = os.path.dirname(os.path.realpath(__file__))
DATA_FOLDER_PATH = os.path.join(CURRENT_FOLDER_PATH, "temp_data")
LABEL_SEARCH_FILE_PATH = os.path.join(CURRENT_FOLDER_PATH, "label_search.json")
LABEL_RELEASES_FILE_PATH = os.path.join(CURRENT_FOLDER_PATH, "label_releases.json")
LIBRARY_FOLDER_PATHS = (
    "Labels/! Example Recordings/1 Singles/[EXR 042] Test Artist - Night Signals EP (2024) [WEB-FLAC]",
    "Labels/! Example Recordings/1 Singles/2023 another_artist-dubplate_pressure-exr041-web",
    "Labels/! Example Recordings/2 Albums/2020-04-10 Test Artist - Long Player (EXRCD017)",
    "Labels/! Example Recordings/2 Albums/2020-04-10 Test Artist - Long Player (EXRCD017)/CD1",
    "Labels/! Example Recordings/1 Singles/Example Recordings - (2025) Thirty Years Of Examples - Part 1",
    "Labels/! Example Recordings/1 Singles/Test Artist - Series - Part 1"
)


def read_response(file_path):
    with open(file_path, "rb") as file_handle:
        return file_handle.read()


@patch("pynicotine.discogs.urlopen")
class DiscogsTest(TestCase):

    # pylint: disable=protected-access

    def setUp(self):

        config.set_data_folder(DATA_FOLDER_PATH)
        config.set_config_file(os.path.join(DATA_FOLDER_PATH, "temp_config"))

        core.init_components(enabled_components={"discogs"})
        core.start()

    def tearDown(self):
        core.quit()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(DATA_FOLDER_PATH)

    @staticmethod
    def set_response(mock_urlopen, file_path):
        mock_urlopen.return_value.__enter__.return_value.read.return_value = read_response(file_path)

    @staticmethod
    def get_request(mock_urlopen):

        request = mock_urlopen.call_args.args[0]
        url = urlsplit(request.full_url)

        return request, url.path, parse_qs(url.query)

    def test_search_labels(self, mock_urlopen):

        self.set_response(mock_urlopen, LABEL_SEARCH_FILE_PATH)
        labels = core.discogs.search_labels("example")

        self.assertEqual([(label.id, label.name) for label in labels], [
            (1001, "Example Recordings"),
            (1002, "Example Recordings Ltd.")
        ])

        request, path, params = self.get_request(mock_urlopen)

        self.assertEqual(path, "/database/search")
        self.assertEqual(params["q"], ["example"])
        self.assertEqual(params["type"], ["label"])
        self.assertTrue(request.get_header("User-agent").startswith("Nicotine+/"))
        self.assertIsNone(request.get_header("Authorization"))

    def test_get_label_releases(self, mock_urlopen):

        self.set_response(mock_urlopen, LABEL_RELEASES_FILE_PATH)
        num_pages, releases = core.discogs.get_label_releases(1001, page=2)

        self.assertEqual(num_pages, 3)
        self.assertEqual(
            [(release.id, release.artist, release.title, release.year, release.catno) for release in releases], [
                (9003, "Test Artist (2)", "Night Signals EP", 2024, "EXR042"),
                (9002, "Another Artist", "Dubplate Pressure", 2023, "EXR041"),
                (9001, "Various", "First Light", None, "EXR001, exr001")
            ]
        )
        self.assertEqual(releases[0].release_format, "12\", EP")

        _request, path, params = self.get_request(mock_urlopen)

        self.assertEqual(path, "/labels/1001/releases")
        self.assertEqual(params["page"], ["2"])
        self.assertEqual(params["sort"], ["year"])
        self.assertEqual(params["sort_order"], ["desc"])

    def test_token(self, mock_urlopen):
        """The token from config is read on the main thread and sent in the
        Authorization header."""

        self.set_response(mock_urlopen, LABEL_SEARCH_FILE_PATH)
        config.sections["discogs"]["token"] = " abc123 "

        with patch("pynicotine.discogs.Discogs._start_thread") as mock_start_thread:
            core.discogs.request_label_search("example")

        target, query, token = mock_start_thread.call_args.args
        target(query, token)

        request, _path, _params = self.get_request(mock_urlopen)
        self.assertEqual(request.get_header("Authorization"), "Discogs token=abc123")

    def test_empty_query(self, _mock_urlopen):

        with patch("pynicotine.discogs.Discogs._start_thread") as mock_start_thread:
            core.discogs.request_label_search("   ")

        mock_start_thread.assert_not_called()

    def test_label_search_event(self, mock_urlopen):

        self.set_response(mock_urlopen, LABEL_SEARCH_FILE_PATH)

        with patch("pynicotine.events.Events.emit_main_thread") as mock_emit:
            core.discogs._label_search("example", token="")

        event_name, query, labels, error_message = mock_emit.call_args.args

        self.assertEqual(event_name, "discogs-label-search")
        self.assertEqual(query, "example")
        self.assertEqual(len(labels), 2)
        self.assertIsNone(error_message)

    def test_label_releases_error_event(self, mock_urlopen):
        """A failed request still emits an event, with an error message and
        no releases."""

        error = HTTPError(url="", code=429, msg="Too Many Requests", hdrs=None, fp=io.BytesIO())
        self.addCleanup(error.close)
        mock_urlopen.side_effect = error

        with patch("pynicotine.events.Events.emit_main_thread") as mock_emit:
            core.discogs._label_releases(1001, 1, token="")

        event_name, label_id, page, num_pages, releases, error_message = mock_emit.call_args.args

        self.assertEqual(event_name, "discogs-label-releases")
        self.assertEqual((label_id, page, num_pages, releases), (1001, 1, 0, []))
        self.assertIn("429", error_message)

    def test_get_search_term(self, _mock_urlopen):

        for (artist, title), search_term in (
            (("Test Artist (2)", "Night Signals EP"), "Test Artist Night Signals EP"),
            (("Immerse (2), Philth (4), Ant TC1*", "Dark '97 / The Eastern"),
             "Immerse, Philth, Ant TC1 Dark '97 / The Eastern"),
            (("Rufige Kru", "Terminator EP (2025 Remastered)"), "Rufige Kru Terminator EP"),
            (("Another Artist", "First Light [Deluxe Edition] (Remaster)"), "Another Artist First Light"),
            (("Various", "Compilation Vol. 1"), "Compilation Vol. 1"),
            (("Various Artists", "Compilation Vol. 2"), "Compilation Vol. 2"),
            (("Test Artist", "Track (Original Mix)"), "Test Artist Track (Original Mix)"),
            (("Test Artist", 'Side A - "Quoted" -ism *star'), "Test Artist Side A Quoted ism star")
        ):
            with self.subTest(artist=artist, title=title):
                self.assertEqual(core.discogs.get_search_term(artist, title), search_term)


class LibraryIndexTest(TestCase):

    def setUp(self):

        self.library = LibraryIndex()

        for folder_path in LIBRARY_FOLDER_PATHS:
            self.library.add_folder(folder_path)

    def test_find_release(self):

        for (artist, title, catno), folder_index in (
            # Catalogue number, written with a space, a dash or in lowercase in the folder name
            (("Test Artist (2)", "Night Signals EP", "EXR042"), 0),
            (("Another Artist", "Dubplate Pressure", "EXR041"), 1),
            (("Another Artist", "Unrelated Title", "XYZ1, EXR041"), 1),

            # Artist and title words, when the catalogue number differs
            (("Test Artist", "Long Player", "EXRLP017"), 2),

            # Distinctive title alone, when the folder names the label instead of the artists
            (("Artist One, Artist Two", "Thirty Years Of Examples - Part 1", "EXR30VIP001"), 4),

            # Numbers must match, so Part 2 is not mistaken for Part 1
            (("Test Artist", "Series - Part 2", "none"), None),

            # Short catalogue numbers like "CD1" are too ambiguous
            (("Someone Else", "Something", "CD1"), None),

            # Too few words for a reliable match
            (("Various", "Long", ""), None)
        ):
            with self.subTest(artist=artist, title=title, catno=catno):
                expected_path = LIBRARY_FOLDER_PATHS[folder_index] if folder_index is not None else None
                self.assertEqual(self.library.find_release(artist, title, catno), expected_path)


class LibraryScanTest(TestCase):

    def setUp(self):

        config.set_data_folder(DATA_FOLDER_PATH)
        config.set_config_file(os.path.join(DATA_FOLDER_PATH, "temp_config"))

        core.init_components(enabled_components={"discogs"})
        core.start()

        self.library_path = os.path.join(DATA_FOLDER_PATH, "library")

        for folder_path in LIBRARY_FOLDER_PATHS:
            os.makedirs(os.path.join(self.library_path, folder_path), exist_ok=True)

        config.sections["discogs"]["library_folders"] = [self.library_path]
        config.sections["transfers"]["downloaddir"] = os.path.join(DATA_FOLDER_PATH, "downloads")

    def tearDown(self):
        core.quit()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(DATA_FOLDER_PATH, ignore_errors=True)

    def test_scan_library(self):

        self.assertEqual(
            core.discogs.get_library_folders(), [self.library_path, os.path.join(DATA_FOLDER_PATH, "downloads")])

        self.run_scan()

        self.assertFalse(core.discogs.is_scanning_library)
        self.assertEqual(
            core.discogs.find_in_library("Test Artist", "Night Signals EP", "EXR042"),
            os.path.join(self.library_path, LIBRARY_FOLDER_PATHS[0])
        )

        # A finished download is added without a new scan
        core.discogs._folder_download_finished(  # pylint: disable=protected-access
            os.path.join(DATA_FOLDER_PATH, "downloads", "New Artist - Fresh Release (NEW001)"))

        self.assertIsNotNone(core.discogs.find_in_library("New Artist", "Fresh Release", "NEW001"))

    def test_rescan_while_scanning(self):
        """Requesting a scan during a scan starts another one afterwards, so
        that newly chosen folders are included."""

        with patch("pynicotine.discogs.Discogs._start_thread") as mock_start_thread:
            core.discogs.request_library_scan()
            core.discogs.request_library_scan()

            self.assertEqual(mock_start_thread.call_count, 1)

            core.discogs._library_scanned(LibraryIndex())  # pylint: disable=protected-access

            self.assertEqual(mock_start_thread.call_count, 2)
            self.assertTrue(core.discogs.is_scanning_library)

    def run_scan(self):
        """Run a library scan on this thread, and return the libraries passed
        to the main thread callbacks (from the cache, then from the scan)."""

        with patch("pynicotine.discogs.Discogs._start_thread") as mock_start_thread:
            core.discogs.request_library_scan()

        target, *args = mock_start_thread.call_args.args

        with patch("pynicotine.events.Events.invoke_main_thread") as mock_invoke_main_thread:
            target(*args)

        libraries = []

        for call in mock_invoke_main_thread.call_args_list:
            callback, library = call.args
            callback(library)
            libraries.append(library)

        return libraries

    def test_library_cache(self):

        # pylint: disable=protected-access

        # First session: no cache yet, only the scan result
        self.assertEqual(len(self.run_scan()), 1)
        num_folders = len(core.discogs.library.folder_paths)

        # Next session: the cache is loaded first, then the scan replaces it
        core.discogs.library = None
        cached_library, scanned_library = self.run_scan()

        self.assertEqual(cached_library.folder_paths, scanned_library.folder_paths)
        self.assertEqual(len(cached_library.folder_paths), num_folders)
        self.assertIs(core.discogs.library, scanned_library)

        # The cache is ignored when the library folders have changed
        core.discogs.library = None
        config.sections["discogs"]["library_folders"] = [os.path.join(self.library_path, "Labels")]

        self.assertEqual(len(self.run_scan()), 1)

        # A broken cache file is ignored
        cache_file_path = os.path.join(DATA_FOLDER_PATH, core.discogs.LIBRARY_CACHE_FILE_NAME)

        with open(cache_file_path, "w", encoding="utf-8") as file_handle:
            file_handle.write("{broken")

        core.discogs.library = None
        self.assertEqual(len(self.run_scan()), 1)


class FolderRenameTest(TestCase):

    # pylint: disable=protected-access

    def setUp(self):

        config.set_data_folder(DATA_FOLDER_PATH)
        config.set_config_file(os.path.join(DATA_FOLDER_PATH, "temp_config"))

        core.init_components(enabled_components={"discogs"})
        core.start()

        self.download_path = os.path.join(DATA_FOLDER_PATH, "rename_downloads")
        config.sections["transfers"]["downloaddir"] = self.download_path
        config.sections["discogs"]["folder_name_pattern"] = "$year $catno $artist - $title"
        config.sections["discogs"]["pending_releases"] = []

    def tearDown(self):
        core.quit()
        shutil.rmtree(self.download_path, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(DATA_FOLDER_PATH, ignore_errors=True)

    def create_folder(self, *folder_names, file_names=()):

        folder_path = os.path.join(self.download_path, *folder_names)
        os.makedirs(folder_path)

        for file_name in file_names:
            with open(os.path.join(folder_path, file_name), "w", encoding="utf-8"):
                pass

        return folder_path

    def test_get_folder_name(self):

        pattern = "$year $catno $artist - $title"

        self.assertEqual(
            core.discogs.get_folder_name(pattern, "Calibre (2)", "Even If: Part 2", 2024, "SIG042"),
            "2024 SIG042 Calibre - Even If_ Part 2")
        self.assertEqual(
            core.discogs.get_folder_name(pattern, "Artist*", "Title", 0, "none"), "Artist - Title")

    def test_rename_downloaded_folder(self):

        core.discogs.add_pending_release("Other Artist", "Unrelated Release", 2020, "OTH001")
        core.discogs.add_pending_release("Test Artist", "Night Signals", 2024, "EXR 042")

        # The title is only in the file names
        folder_path = self.create_folder("tunes", file_names=("01 night_signals.flac",))
        core.discogs._folder_download_finished(folder_path)

        new_folder_path = os.path.join(self.download_path, "2024 EXR 042 Test Artist - Night Signals")

        self.assertTrue(os.path.isdir(new_folder_path))
        self.assertFalse(os.path.exists(folder_path))
        self.assertEqual(
            config.sections["discogs"]["pending_releases"], [["Other Artist", "Unrelated Release", 2020, "OTH001"]])

    def test_keep_folder_name(self):

        core.discogs.add_pending_release("Test Artist", "Night Signals", 2024, "EXR042")

        # Not a pending release
        folder_path = self.create_folder("Something Else")
        core.discogs._folder_download_finished(folder_path)
        self.assertTrue(os.path.isdir(folder_path))

        # Subfolders of a release are not renamed
        folder_path = self.create_folder("Night Signals", "CD1", file_names=("night signals.flac",))
        core.discogs._folder_download_finished(folder_path)
        self.assertTrue(os.path.isdir(folder_path))

        # Renaming is turned off
        config.sections["discogs"]["folder_name_pattern"] = ""
        folder_path = self.create_folder("Night Signals Download")
        core.discogs._folder_download_finished(folder_path)
        self.assertTrue(os.path.isdir(folder_path))
        self.assertEqual(len(config.sections["discogs"]["pending_releases"]), 1)
