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

CURRENT_FOLDER_PATH = os.path.dirname(os.path.realpath(__file__))
DATA_FOLDER_PATH = os.path.join(CURRENT_FOLDER_PATH, "temp_data")
LABEL_SEARCH_FILE_PATH = os.path.join(CURRENT_FOLDER_PATH, "label_search.json")
LABEL_RELEASES_FILE_PATH = os.path.join(CURRENT_FOLDER_PATH, "label_releases.json")


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
