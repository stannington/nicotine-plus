# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import re
import string
import threading
import time

from urllib.parse import quote
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen

import pynicotine
from pynicotine.config import config
from pynicotine.events import events
from pynicotine.logfacility import log
from pynicotine.utils import clean_file
from pynicotine.utils import encode_path


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


class DiscogsReleaseGroup:
    """Formats of the same release, e.g. the vinyl, FLAC and WAV versions,
    shown as one row. Has the same attributes as DiscogsRelease."""

    __slots__ = ("releases",)

    QUANTITY_PATTERN = re.compile(r"^\d+x")  # "2x12"" or "16xFile"

    def __init__(self, releases):
        self.releases = releases

    @property
    def id(self):
        # The first release stays first when more pages are loaded, so the id doesn't change
        return self.releases[0].id

    @property
    def artist(self):
        return self.releases[0].artist

    @property
    def title(self):
        return self.releases[0].title

    @property
    def year(self):
        """The earliest year of the formats."""
        return min((release.year for release in self.releases if release.year), default=None)

    @property
    def catno(self):
        """All catalogue numbers, the most common one first. Formats often
        share one, written differently, e.g. "META 111" and "META111"."""

        catnos = {}

        for release in self.releases:
            for catno in release.catno.split(","):
                catno = catno.strip()
                key = Discogs.CATNO_SEPARATOR_PATTERN.sub("", catno).lower()

                if not key:
                    continue

                if key in catnos:
                    catnos[key][1] += 1
                else:
                    catnos[key] = [catno, 1]

        if len(catnos) > 1:
            # Discogs uses "none" for releases without a catalogue number
            catnos.pop("none", None)

        # Sorting is stable, so equally common ones keep their order
        return ", ".join(catno for catno, _count in sorted(catnos.values(), key=lambda item: -item[1]))

    @property
    def release_format(self):
        """Formats without duplicates, e.g. "LP, Album, FLAC, WAV"."""

        format_names = {}

        for release in self.releases:
            for format_name in release.release_format.split(","):
                format_name = self.QUANTITY_PATTERN.sub("", format_name.strip())

                if format_name and format_name != "File":
                    format_names.setdefault(format_name.lower(), format_name)

        if not format_names and any(release.release_format for release in self.releases):
            return "File"

        return ", ".join(format_names.values())

    def is_new(self, last_seen_release_id):
        """Check if all formats were added since the label was last viewed,
        so that a new format of an older release doesn't count as new."""

        if last_seen_release_id is None:
            return False

        return all(release.id > last_seen_release_id for release in self.releases)


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
            (self.get_significant_words(f"{artist} {title}"), self.MIN_SIGNIFICANT_WORDS),
            (self.get_significant_words(title), self.MIN_SIGNIFICANT_TITLE_WORDS)
        ):
            if len(words) < min_words:
                # Too few words for a reliable match
                continue

            folder_index = self._find_folder_with_words(words)

            if folder_index is not None:
                return self.folder_paths[folder_index]

        return None

    @classmethod
    def get_significant_words(cls, text):
        return {
            word for word in cls.get_words(text)
            # Keep short numbers, so that e.g. "Part 4" doesn't match "Part 1"
            if (len(word) >= 3 or word.isdigit()) and word not in cls.IGNORED_WORDS
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

    __slots__ = ("library", "is_scanning_library", "is_checking_favourite_labels",
                 "labels_with_new_releases", "_should_rescan_library")

    API_URL = "https://api.discogs.com"
    MAX_LABEL_RESULTS = 50
    RELEASES_PER_PAGE = 100  # Maximum allowed by Discogs
    TIMEOUT = 10

    # Seconds between requests when checking favourite labels, to leave room
    # in the rate limit for browsing the tab meanwhile
    CHECK_DELAY = 1.5
    CHECK_DELAY_WITHOUT_TOKEN = 3

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
    MAX_PENDING_RELEASES = 100
    MAX_FORMAT_YEAR_DIFFERENCE = 1  # Formats years apart are reissues, and kept apart
    OLD_DEFAULT_FOLDER_NAME_PATTERN = "$year $catno $artist - $title"
    CATNO_SEPARATOR_PATTERN = re.compile(r"[\s\-\u2010-\u2015]+")  # Spaces and dashes
    # Years not joined to letters or digits, so that e.g. "EXR1995" doesn't count
    YEAR_PATTERN = re.compile(r"(?<![^\W_])(?:19|20)\d\d(?![^\W_])")

    def __init__(self):

        self.library = None
        self.is_scanning_library = False
        self.is_checking_favourite_labels = False
        self.labels_with_new_releases = set()
        self._should_rescan_library = False

        if config.sections["discogs"]["folder_name_pattern"] == self.OLD_DEFAULT_FOLDER_NAME_PATTERN:
            # Upgrade the previous default pattern to the new one with a label folder
            config.sections["discogs"]["folder_name_pattern"] = config.defaults["discogs"]["folder_name_pattern"]

        for event_name, callback in (
            ("folder-download-finished", self._folder_download_finished),
            ("start", self._start)
        ):
            events.connect(event_name, callback)

    def _start(self):
        self.request_favourite_labels_check()

    def _folder_download_finished(self, folder_path):

        folder_path = self._rename_downloaded_folder(folder_path)

        if self.library is None:
            return

        self.library.add_folder(folder_path)
        events.emit("discogs-library-updated")

    def is_earlier_version(self, folder_path, year):
        """Check if a library folder is likely an earlier version of a
        release from the given year, e.g. the original of a reissue. Only
        folders with a year in their name can be checked."""

        if not year:
            return False

        folder_name = os.path.basename(os.path.normpath(folder_path))
        folder_years = [int(folder_year) for folder_year in self.YEAR_PATTERN.findall(folder_name)]

        return bool(folder_years) and max(folder_years) < year

    def get_catno_search_term(self, catno):
        """Create a Soulseek search term for a release's catalogue number,
        or return None if it has none."""

        for single_catno in catno.split(","):
            # Same rules as for artist and title search terms
            search_term = self.get_search_term("", single_catno)

            if search_term and search_term.lower() != "none":
                return search_term

        return None

    def is_favourite_label(self, label_id):
        return any(favourite_id == label_id for favourite_id, _name in config.sections["discogs"]["favourite_labels"])

    def add_favourite_label(self, label_id, name):

        if not self.is_favourite_label(label_id):
            config.sections["discogs"]["favourite_labels"].append([label_id, name])
            config.write_configuration()

    def remove_favourite_label(self, label_id):

        config.sections["discogs"]["favourite_labels"] = [
            favourite for favourite in config.sections["discogs"]["favourite_labels"] if favourite[0] != label_id
        ]
        config.write_configuration()

    def get_last_seen_release_id(self, label_id):
        """Return the highest release id seen for a label, or None if the
        label was never viewed. Discogs ids increase as releases are added,
        so releases with a higher id were added since."""

        return config.sections["discogs"]["last_seen_releases"].get(label_id)

    def set_last_seen_release_id(self, label_id, release_id):

        last_seen_releases = config.sections["discogs"]["last_seen_releases"]

        if release_id > last_seen_releases.get(label_id, 0):
            last_seen_releases[label_id] = release_id

        # The newest releases were just seen
        self.labels_with_new_releases.discard(label_id)

    def request_favourite_labels_check(self):
        """Check each favourite label for releases added since it was last
        viewed, in the background. Emits "discogs-favourite-label-checked"
        with the label id and whether it has new releases, for each label,
        then "discogs-favourite-labels-checked" when done.

        Only the newest page of releases is checked, one label at a time, to
        stay within the rate limit.
        """

        favourite_label_ids = [label_id for label_id, _name in config.sections["discogs"]["favourite_labels"]]

        if self.is_checking_favourite_labels or not favourite_label_ids:
            return

        token = self._get_token()
        delay = self.CHECK_DELAY if token else self.CHECK_DELAY_WITHOUT_TOKEN

        self.is_checking_favourite_labels = True
        self._start_thread(self._check_favourite_labels, favourite_label_ids, token, delay)

    def _check_favourite_labels(self, label_ids, token, delay):

        for index, label_id in enumerate(label_ids):
            if index:
                time.sleep(delay)

            try:
                _num_pages, releases = self.get_label_releases(label_id, token=token)

            except Exception as error:
                log.add_debug("Unable to check Discogs label %s for new releases: %s", (label_id, error))
                continue

            events.invoke_main_thread(self._favourite_label_checked, label_id, releases)

        events.invoke_main_thread(self._favourite_labels_checked)

    def _favourite_label_checked(self, label_id, releases):

        # Compared on the main thread, in case the label was viewed meanwhile
        last_seen_release_id = self.get_last_seen_release_id(label_id)
        has_new_releases = any(group.is_new(last_seen_release_id) for group in self.group_releases(releases))

        if has_new_releases:
            self.labels_with_new_releases.add(label_id)
        else:
            self.labels_with_new_releases.discard(label_id)

        events.emit("discogs-favourite-label-checked", label_id, has_new_releases)

    def _favourite_labels_checked(self):
        self.is_checking_favourite_labels = False
        events.emit("discogs-favourite-labels-checked")

    def add_pending_release(self, artist, title, year, catno, label=""):
        """Remember a release searched for from the Discogs tab, so that its
        folder can be renamed once it's downloaded."""

        pending_releases = config.sections["discogs"]["pending_releases"]
        release = [artist, title, year or 0, catno, label]

        if release in pending_releases:
            pending_releases.remove(release)

        pending_releases.append(release)
        del pending_releases[:-self.MAX_PENDING_RELEASES]

    def get_folder_name(self, pattern, artist, title, year, catno, label=""):
        """Create a folder name for a release from a pattern such as
        "$label/$year $catno $artist - $title". Each "/" in the pattern
        creates a subfolder, so the result can be a relative path."""

        artist = self.ARTIST_NUMBER_PATTERN.sub("", artist).replace("*", "")
        label = self.ARTIST_NUMBER_PATTERN.sub("", label)

        # Use the first catalogue number as one block, e.g. "DSCI4 047" as "DSCI4047"
        catno = self.CATNO_SEPARATOR_PATTERN.sub("", catno.split(",", 1)[0])

        if catno.lower() == "none":
            # Discogs uses "none" for releases without a catalogue number
            catno = ""

        folder_names = []

        for pattern_part in pattern.split("/"):
            folder_name = string.Template(pattern_part).safe_substitute(
                year=year or "", catno=catno, artist=artist, title=title, label=label)

            # Remove extra spaces left by empty values
            folder_name = " ".join(folder_name.split())

            if folder_name:
                # Skip empty parts, e.g. no label for releases searched before labels were saved
                folder_names.append(clean_file(folder_name))

        return os.path.join(*folder_names) if folder_names else ""

    def _find_pending_release(self, folder_path):
        """Return the pending release whose title words or catalogue number
        are in the folder name or its file names, or None."""

        try:
            names = os.listdir(encode_path(folder_path))

        except OSError as error:
            log.add_debug("Unable to read downloaded folder %s: %s", (folder_path, error))
            return None

        words = set(LibraryIndex.get_words(os.path.basename(folder_path)))

        # Index the folder and file names, to find catalogue numbers in them
        name_index = LibraryIndex()
        name_index.add_folder(folder_path)

        for name in names:
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")

            words.update(LibraryIndex.get_words(name))
            name_index.add_folder(name)

        # Most recently searched first
        for release in reversed(config.sections["discogs"]["pending_releases"]):
            artist, title, _year, catno, *_label = release
            title_words = LibraryIndex.get_significant_words(title)

            if title_words and title_words <= words:
                return release

            if name_index.find_release(artist, title, catno) is not None:
                return release

        return None

    def _is_top_download_folder(self, folder_path):
        """Check that the folder is a release folder directly in the
        download folder, and not e.g. a "CD1" subfolder of a release."""

        download_folder_path = os.path.normpath(os.path.expandvars(config.sections["transfers"]["downloaddir"]))
        parent_folder_path = os.path.dirname(os.path.normpath(folder_path))

        if parent_folder_path == download_folder_path:
            return True

        # Folders can be in a subfolder per user
        return (config.sections["transfers"]["usernamesubfolders"]
                and os.path.dirname(parent_folder_path) == download_folder_path)

    def _rename_downloaded_folder(self, folder_path):
        """Rename a downloaded folder of a release searched for from the
        Discogs tab, using the folder name pattern. Return the new path, or
        the old path if the folder was not renamed."""

        pattern = config.sections["discogs"]["folder_name_pattern"].strip()

        if not pattern or not config.sections["discogs"]["pending_releases"]:
            return folder_path

        if not self._is_top_download_folder(folder_path):
            return folder_path

        release = self._find_pending_release(folder_path)

        if release is None:
            return folder_path

        folder_name = self.get_folder_name(pattern, *release)

        if not folder_name:
            return folder_path

        new_folder_path = os.path.join(os.path.dirname(folder_path), folder_name)

        if new_folder_path == folder_path:
            config.sections["discogs"]["pending_releases"].remove(release)
            return folder_path

        if os.path.exists(encode_path(new_folder_path)):
            log.add(_("Cannot rename downloaded folder %(old)s to %(new)s: a folder with that name already "
                      "exists"), {"old": folder_path, "new": folder_name})
            return folder_path

        try:
            # Create the label folder, if the pattern has one
            os.makedirs(encode_path(os.path.dirname(new_folder_path)), exist_ok=True)
            os.rename(encode_path(folder_path), encode_path(new_folder_path))

        except OSError as error:
            log.add(_("Cannot rename downloaded folder %(old)s to %(new)s: %(error)s"), {
                "old": folder_path, "new": folder_name, "error": error})
            return folder_path

        config.sections["discogs"]["pending_releases"].remove(release)
        log.add(_("Renamed downloaded folder %(old)s to %(new)s"), {"old": folder_path, "new": folder_name})

        return new_folder_path

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

    def group_releases(self, releases):
        """Group the formats of each release, newest first. Releases are
        formats of the same release if they have the same artist and title,
        and were released within a year of the newest one. Releases are
        expected newest first, as Discogs returns them."""

        groups = []
        groups_by_name = {}
        unknown_year_releases = []

        for release in releases:
            if not release.year:
                unknown_year_releases.append(release)
                continue

            name = (tuple(LibraryIndex.get_words(release.artist)), tuple(LibraryIndex.get_words(release.title)))
            group = None

            for candidate_group in groups_by_name.get(name, ()):
                if candidate_group.releases[0].year - release.year <= self.MAX_FORMAT_YEAR_DIFFERENCE:
                    group = candidate_group
                    break

            if group is None:
                group = DiscogsReleaseGroup([])
                groups.append(group)
                groups_by_name.setdefault(name, []).append(group)

            group.releases.append(release)

        for release in unknown_year_releases:
            name = (tuple(LibraryIndex.get_words(release.artist)), tuple(LibraryIndex.get_words(release.title)))
            candidate_groups = groups_by_name.get(name, [])

            if len(candidate_groups) == 1:
                # Only one version of the release, so this is likely another format of it
                candidate_groups[0].releases.append(release)
                continue

            group = DiscogsReleaseGroup([release])
            groups.append(group)
            groups_by_name.setdefault(name, []).append(group)

        # Sort by the earliest year of the formats, unknown years last. Sorting
        # is stable, so releases from the same year keep the Discogs order.
        groups.sort(key=lambda group: -(group.year or 0))
        return groups

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
