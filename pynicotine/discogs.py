# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import re
import threading

from urllib.parse import quote
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen

import pynicotine
from pynicotine.config import config
from pynicotine.events import events
from pynicotine.logfacility import log


class DiscogsLabel:
    __slots__ = ("id", "name")

    def __init__(self, label_id, name):
        self.id = label_id
        self.name = name


class DiscogsRelease:
    __slots__ = ("id", "artist", "title", "year", "catno", "release_format")

    def __init__(self, release_id, artist, title, year, catno, release_format):
        self.id = release_id
        self.artist = artist
        self.title = title
        self.year = year
        self.catno = catno
        self.release_format = release_format


class LibraryIndex:
    """Folder names in the music library, indexed so that a Discogs release
    can be matched by catalogue number, or else by artist and title words."""

    __slots__ = ("folder_paths", "_catnos", "_words")

    WORD_PATTERN = re.compile(r"[^\W_]+")  # Letters and digits, split on anything else
    MAX_CATNO_WORDS = 3                    # "METH 056" is split into two words
    MIN_CATNO_LENGTH = 4                   # Shorter ones, e.g. "CD1", are too ambiguous
    MIN_SIGNIFICANT_WORDS = 2
    MIN_SIGNIFICANT_TITLE_WORDS = 3
    IGNORED_WORDS = {"the", "and", "various", "artists", "vol", "vip", "mix", "remix", "edit", "dub",
                     "ep", "lp", "cd", "web", "flac", "mp3", "wav", "vinyl"}

    def __init__(self):
        self.folder_paths = []
        self._catnos = {}
        self._words = {}

    @classmethod
    def get_words(cls, text):
        return cls.WORD_PATTERN.findall(text.lower())

    @staticmethod
    def _is_catno(text):
        return any(char.isdigit() for char in text) and any(char.isalpha() for char in text)

    def add_folder(self, folder_path):

        folder_index = len(self.folder_paths)
        folder_name = folder_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        words = self.get_words(folder_name)

        self.folder_paths.append(folder_path)

        for word in words:
            self._words.setdefault(word, set()).add(folder_index)

        # Catalogue numbers can be written with separators, e.g. "META 066" or
        # "meta-066", so also join neighbouring words
        for start_index in range(len(words)):
            for end_index in range(start_index + 1, min(start_index + self.MAX_CATNO_WORDS, len(words)) + 1):
                catno = "".join(words[start_index:end_index])

                if self._is_catno(catno):
                    self._catnos.setdefault(catno, folder_index)

    def find_release(self, artist, title, catno):
        """Return the path of a folder that likely contains the release, or
        None."""

        # Discogs separates multiple catalogue numbers with commas
        for single_catno in catno.split(","):
            single_catno = "".join(self.get_words(single_catno))

            if len(single_catno) < self.MIN_CATNO_LENGTH or not self._is_catno(single_catno):
                # Not a usable catalogue number, e.g. "none"
                continue

            folder_index = self._catnos.get(single_catno)

            if folder_index is not None:
                return self.folder_paths[folder_index]

        # No catalogue number match, look for a folder containing all words of
        # the artist and title. Folders sometimes name the label instead of the
        # artists, so also try the title alone if it's distinctive enough.
        artist = Discogs.ARTIST_NUMBER_PATTERN.sub("", artist)

        for words, min_words in (
            (self._get_significant_words(f"{artist} {title}"), self.MIN_SIGNIFICANT_WORDS),
            (self._get_significant_words(title), self.MIN_SIGNIFICANT_TITLE_WORDS)
        ):
            if len(words) < min_words:
                # Too few words for a reliable match
                continue

            folder_index = self._find_folder_with_words(words)

            if folder_index is not None:
                return self.folder_paths[folder_index]

        return None

    def _get_significant_words(self, text):
        return {
            word for word in self.get_words(text)
            # Keep short numbers, so that e.g. "Part 4" doesn't match "Part 1"
            if (len(word) >= 3 or word.isdigit()) and word not in self.IGNORED_WORDS
        }

    def _find_folder_with_words(self, words):

        folder_indexes = None

        # Start with the rarest word, to keep the intersections small
        for word in sorted(words, key=lambda word: len(self._words.get(word, ()))):
            word_folder_indexes = self._words.get(word)

            if not word_folder_indexes:
                return None

            if folder_indexes is None:
                folder_indexes = set(word_folder_indexes)
            else:
                folder_indexes &= word_folder_indexes

            if not folder_indexes:
                return None

        return min(folder_indexes)


class Discogs:
    """Looks up record labels and their releases using the Discogs API.

    Every request runs on its own thread, and the result is passed back to
    the main thread as an event. Each request method makes a single API call,
    to stay within the Discogs rate limit (25 requests per minute, or 60 with
    a personal access token).
    """

    __slots__ = ("library", "is_scanning_library", "_should_rescan_library")

    API_URL = "https://api.discogs.com"
    MAX_LABEL_RESULTS = 50
    RELEASES_PER_PAGE = 100  # Maximum allowed by Discogs
    TIMEOUT = 10

    # Number Discogs adds to tell apart artists with the same name, e.g. "Calibre (2)"
    ARTIST_NUMBER_PATTERN = re.compile(r"\s*\(\d+\)")

    # Edition notes in titles, e.g. "(2025 Remastered)" or "[Deluxe Edition]"
    EDITION_NOTE_PATTERN = re.compile(
        r"\s*[(\[][^)\]]*\b(?:remaster|remastered|reissue|repress|deluxe|expanded|anniversary|edition)\b"
        r"[^)\]]*[)\]]",
        flags=re.IGNORECASE
    )
    VARIOUS_ARTISTS = {"various", "various artists"}
    LIBRARY_CACHE_FILE_NAME = "discogs_library.json"
    LIBRARY_CACHE_VERSION = 1

    def __init__(self):

        self.library = None
        self.is_scanning_library = False
        self._should_rescan_library = False

        events.connect("folder-download-finished", self._folder_download_finished)

    def _folder_download_finished(self, folder_path):

        if self.library is None:
            return

        self.library.add_folder(folder_path)
        events.emit("discogs-library-updated")

    def get_library_folders(self):
        """Folders to scan: the chosen library folders and the download
        folder."""

        folder_paths = []
        download_folder_path = config.sections["transfers"]["downloaddir"]

        for folder_path in (*config.sections["discogs"]["library_folders"], download_folder_path):
            if not folder_path:
                continue

            # The download folder can contain variables, e.g. ${NICOTINE_DATA_HOME}
            folder_path = os.path.normpath(os.path.expandvars(folder_path))

            if folder_path not in folder_paths:
                folder_paths.append(folder_path)

        return folder_paths

    def request_library_scan(self):
        """Scan the library folders for release folders in the background.
        Emits "discogs-library-updated" when done.

        The first scan in a session also loads the results of the previous
        scan from a cache file, so that releases can be ticked before the
        slower scan finishes. That also emits "discogs-library-updated".
        """

        if self.is_scanning_library:
            # Folders may have changed, scan again when the current scan is done
            self._should_rescan_library = True
            return

        self.is_scanning_library = True
        self._start_thread(
            self._scan_library, self.get_library_folders(),
            os.path.join(config.data_folder_path, self.LIBRARY_CACHE_FILE_NAME), self.library is None
        )

    def _scan_library(self, library_folder_paths, cache_file_path, use_cache):

        if use_cache:
            library = self._load_library_cache(cache_file_path, library_folder_paths)

            if library is not None:
                events.invoke_main_thread(self._library_cache_loaded, library)

        library = LibraryIndex()

        for library_folder_path in library_folder_paths:
            for current_path, folder_names, _file_names in os.walk(library_folder_path, onerror=self._scan_error):
                folder_names.sort()

                for folder_name in folder_names:
                    library.add_folder(os.path.join(current_path, folder_name))

        self._save_library_cache(cache_file_path, library_folder_paths, library)
        events.invoke_main_thread(self._library_scanned, library)

    @staticmethod
    def _scan_error(error):
        log.add_debug("Unable to scan Discogs library folder: %s", error)

    def _load_library_cache(self, cache_file_path, library_folder_paths):

        try:
            with open(cache_file_path, encoding="utf-8") as file_handle:
                cache = json.load(file_handle)

        except FileNotFoundError:
            return None

        except (OSError, ValueError) as error:
            log.add_debug("Unable to load Discogs library cache: %s", error)
            return None

        if (not isinstance(cache, dict) or cache.get("version") != self.LIBRARY_CACHE_VERSION
                or cache.get("library_folders") != library_folder_paths):
            # Outdated, or the library folders have changed since
            return None

        library = LibraryIndex()

        for folder_path in cache.get("folder_paths", []):
            library.add_folder(folder_path)

        return library

    def _save_library_cache(self, cache_file_path, library_folder_paths, library):

        temp_file_path = f"{cache_file_path}.tmp"

        try:
            with open(temp_file_path, "w", encoding="utf-8") as file_handle:
                json.dump({
                    "version": self.LIBRARY_CACHE_VERSION,
                    "library_folders": library_folder_paths,
                    "folder_paths": library.folder_paths
                }, file_handle)

            # Replace the old file only once the new one is complete
            os.replace(temp_file_path, cache_file_path)

        except OSError as error:
            log.add_debug("Unable to save Discogs library cache: %s", error)

    def _library_cache_loaded(self, library):

        if self.library is not None:
            # The scan already finished
            return

        self.library = library
        events.emit("discogs-library-updated")

    def _library_scanned(self, library):

        self.library = library
        self.is_scanning_library = False

        if self._should_rescan_library:
            self._should_rescan_library = False
            self.request_library_scan()

        events.emit("discogs-library-updated")

    def find_in_library(self, artist, title, catno):
        """Return the path of the library folder that likely contains the
        release, or None if not found or the library is not scanned yet."""

        if self.library is None:
            return None

        return self.library.find_release(artist, title, catno)

    def request_label_search(self, query):
        """Search for labels matching the query. Emits "discogs-label-search"
        with the query, a list of DiscogsLabel and an error message (or None).
        """

        query = query.strip()

        if not query:
            return

        self._start_thread(self._label_search, query, self._get_token())

    def request_label_releases(self, label_id, page=1):
        """Retrieve one page of a label's releases, newest first. Emits
        "discogs-label-releases" with the label id, page number, total number
        of pages, a list of DiscogsRelease and an error message (or None).
        """

        self._start_thread(self._label_releases, label_id, page, self._get_token())

    def get_search_term(self, artist, title):
        """Create a Soulseek search term for a release, without the extra
        details in Discogs names that make searches find fewer results."""

        artist = self.ARTIST_NUMBER_PATTERN.sub("", artist)
        title = self.EDITION_NOTE_PATTERN.sub("", title)

        if artist.strip().lower() in self.VARIOUS_ARTISTS:
            artist = ""

        # Discogs marks alternative artist names with "*". Soulseek also treats
        # quotes, and "-" and "*" at the start of words, as search syntax.
        words = f"{artist} {title}".replace("*", " ").replace('"', " ").split()

        return " ".join(word.lstrip("-") for word in words if word.lstrip("-"))

    @staticmethod
    def _get_token():
        # Read on the main thread, since config is not thread-safe
        return config.sections["discogs"]["token"].strip()

    @staticmethod
    def _start_thread(target, *args):
        # Daemon thread, so that a slow request never delays quitting
        threading.Thread(target=target, args=args, name="Discogs", daemon=True).start()

    def _label_search(self, query, token):

        labels = []
        error_message = None

        try:
            labels = self.search_labels(query, token)

        except Exception as error:
            log.add_debug("Unable to search Discogs labels for %s: %s", (query, error))
            error_message = str(error)

        events.emit_main_thread("discogs-label-search", query, labels, error_message)

    def _label_releases(self, label_id, page, token):

        num_pages = 0
        releases = []
        error_message = None

        try:
            num_pages, releases = self.get_label_releases(label_id, page, token)

        except Exception as error:
            log.add_debug("Unable to retrieve Discogs releases for label %s: %s", (label_id, error))
            error_message = str(error)

        events.emit_main_thread("discogs-label-releases", label_id, page, num_pages, releases, error_message)

    def search_labels(self, query, token=""):

        data = self._request("/database/search", {
            "q": query,
            "type": "label",
            "per_page": self.MAX_LABEL_RESULTS
        }, token)

        return [DiscogsLabel(result["id"], result["title"]) for result in data["results"]]

    def get_label_releases(self, label_id, page=1, token=""):

        # Sorting is not in the API documentation, but Discogs supports it.
        # Releases with an unknown year (0) are placed last.
        data = self._request(f"/labels/{quote(str(label_id))}/releases", {
            "page": page,
            "per_page": self.RELEASES_PER_PAGE,
            "sort": "year",
            "sort_order": "desc"
        }, token)

        releases = []

        for item in data["releases"]:
            releases.append(DiscogsRelease(
                release_id=item["id"],
                artist=item.get("artist", ""),
                title=item.get("title", ""),
                year=item.get("year") or None,
                catno=item.get("catno", ""),
                release_format=item.get("format", "")
            ))

        return data["pagination"]["pages"], releases

    def _request(self, path, params, token):

        headers = {
            "User-Agent": f"{pynicotine.__application_name__}/{pynicotine.__version__} "
                          f"+{pynicotine.__website_url__}"
        }
        if token:
            headers["Authorization"] = f"Discogs token={token}"

        request = Request(f"{self.API_URL}{path}?{urlencode(params)}", headers=headers)

        with urlopen(request, timeout=self.TIMEOUT) as response:
            response_body = response.read().decode("utf-8", "replace")

        return json.loads(response_body)
