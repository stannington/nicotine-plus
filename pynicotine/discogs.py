# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import json
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


class Discogs:
    """Looks up record labels and their releases using the Discogs API.

    Every request runs on its own thread, and the result is passed back to
    the main thread as an event. Each request method makes a single API call,
    to stay within the Discogs rate limit (25 requests per minute, or 60 with
    a personal access token).
    """

    __slots__ = ()

    API_URL = "https://api.discogs.com"
    MAX_LABEL_RESULTS = 50
    RELEASES_PER_PAGE = 100  # Maximum allowed by Discogs
    TIMEOUT = 10

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
