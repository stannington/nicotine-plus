# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import GObject

from pynicotine.core import core
from pynicotine.events import events
from pynicotine.gtkgui.application import GTK_API_VERSION
from pynicotine.gtkgui.widgets import ui
from pynicotine.gtkgui.widgets.popupmenu import PopupMenu
from pynicotine.gtkgui.widgets.treeview import TreeView
from pynicotine.utils import humanize


class Discogs:

    def __init__(self, window):

        (
            self.container,
            self.labels_list_container,
            self.load_more_button,
            self.releases_label,
            self.releases_list_container,
            self.search_entry,
            self.status_label
        ) = ui.load(scope=self, path="discogs.ui")

        if GTK_API_VERSION >= 4:
            window.discogs_container.append(self.container)
        else:
            window.discogs_container.add(self.container)

        self.window = window
        self.page = window.discogs_page
        self.page.id = "discogs"
        self.toolbar = window.discogs_toolbar
        self.toolbar_start_content = window.discogs_title
        self.toolbar_end_content = window.discogs_end
        self.toolbar_default_widget = None

        # Only results for the latest search and selected label are shown,
        # in case an older request finishes after a newer one
        self.search_query = None
        self.label_id = None
        self.page_number = 0
        self.num_pages = 0

        self.labels_list_view = TreeView(
            window, parent=self.labels_list_container,
            select_row_callback=self.on_select_label,
            columns={
                # Visible columns
                "label": {
                    "column_type": "text",
                    "title": _("Label")
                },

                # Hidden data columns
                "id_data": {"data_type": GObject.TYPE_UINT64, "iterator_key": True},
                "order_data": {"data_type": GObject.TYPE_UINT, "default_sort_type": "ascending"}
            }
        )

        self.releases_list_view = TreeView(
            window, parent=self.releases_list_container,
            activate_row_callback=self.on_search_release,
            columns={
                # Visible columns
                "year": {
                    "column_type": "number",
                    "title": _("Year"),
                    "width": 60,
                    "sort_column": "year_data"
                },
                "artist": {
                    "column_type": "text",
                    "title": _("Artist"),
                    "width": 200,
                    "expand_column": True
                },
                "title": {
                    "column_type": "text",
                    "title": _("Title"),
                    "width": 250,
                    "expand_column": True
                },
                "catno": {
                    "column_type": "text",
                    "title": _("Catalogue Number"),
                    "width": 140
                },
                "format": {
                    "column_type": "text",
                    "title": _("Format"),
                    "width": 160,
                    "expand_column": True
                },

                # Hidden data columns
                "id_data": {"data_type": GObject.TYPE_UINT64, "iterator_key": True},
                "year_data": {"data_type": GObject.TYPE_UINT},
                "order_data": {"data_type": GObject.TYPE_UINT, "default_sort_type": "ascending"}
            }
        )

        self.popup_menu = PopupMenu(self.window.application, self.releases_list_view.widget)
        self.popup_menu.add_items(
            ("#" + _("_Search for Release"), self.on_search_release)
        )

        for event_name, callback in (
            ("discogs-label-releases", self.label_releases),
            ("discogs-label-search", self.label_search)
        ):
            events.connect(event_name, callback)

    def destroy(self):

        self.popup_menu.destroy()
        self.labels_list_view.destroy()
        self.releases_list_view.destroy()

        self.__dict__.clear()

    def on_focus(self, *_args):

        self.search_entry.grab_focus()
        return True

    def request_releases(self, page_number):

        self.page_number = page_number
        self.load_more_button.set_sensitive(False)
        self.status_label.set_label(_("Loading…"))

        core.discogs.request_label_releases(self.label_id, page_number)

    def update_status(self):

        num_releases = len(self.releases_list_view.iterators)
        has_more_pages = self.page_number < self.num_pages

        if not num_releases:
            status = _("No releases found")

        elif has_more_pages:
            status = _("Showing the %(num_releases)s newest releases") % {"num_releases": humanize(num_releases)}

        else:
            status = _("Showing all %(num_releases)s releases") % {"num_releases": humanize(num_releases)}

        self.status_label.set_label(status)
        self.load_more_button.set_label(_("Load More"))
        self.load_more_button.set_visible(has_more_pages)
        self.load_more_button.set_sensitive(True)

    def label_search(self, query, labels, error_message):

        if query != self.search_query:
            return

        if error_message:
            self.status_label.set_label(_("Discogs search failed: %(error)s") % {"error": error_message})
            return

        self.labels_list_view.freeze()

        for order, label in enumerate(labels):
            self.labels_list_view.add_row([label.name, label.id, order], select_row=False)

        self.labels_list_view.unfreeze()

        if labels:
            self.status_label.set_label(_("Select a label to see its releases"))
        else:
            self.status_label.set_label(_("No labels found for %(query)s") % {"query": query})

    def label_releases(self, label_id, page_number, num_pages, releases, error_message):

        if label_id != self.label_id or page_number != self.page_number:
            return

        if error_message:
            # Step back, so that the Retry button requests the same page again
            self.page_number -= 1
            self.status_label.set_label(_("Discogs request failed: %(error)s") % {"error": error_message})
            self.load_more_button.set_label(_("Retry"))
            self.load_more_button.set_visible(True)
            self.load_more_button.set_sensitive(True)
            return

        self.num_pages = num_pages
        first_order = (page_number - 1) * core.discogs.RELEASES_PER_PAGE

        self.releases_list_view.freeze()

        for index, release in enumerate(releases):
            # Releases that also appeared on an earlier page are skipped by add_row()
            self.releases_list_view.add_row([
                str(release.year or ""),
                release.artist,
                release.title,
                release.catno,
                release.release_format,
                release.id,
                release.year or 0,
                first_order + index
            ], select_row=False)

        self.releases_list_view.unfreeze()
        self.update_status()

    def on_search_labels(self, *_args):

        query = self.search_entry.get_text().strip()

        if not query:
            return

        self.search_query = query
        self.label_id = None
        self.labels_list_view.clear()
        self.releases_list_view.clear()
        self.releases_label.set_label(_("Releases"))
        self.load_more_button.set_visible(False)
        self.status_label.set_label(_("Searching…"))

        core.discogs.request_label_search(query)

    def on_select_label(self, list_view, iterator):

        if iterator is None:
            return

        label_id = list_view.get_row_value(iterator, "id_data")

        if label_id == self.label_id:
            return

        self.label_id = label_id
        self.num_pages = 0

        self.releases_list_view.clear()
        self.releases_label.set_label(list_view.get_row_value(iterator, "label"))
        self.load_more_button.set_visible(False)
        self.request_releases(page_number=1)

    def on_load_more(self, *_args):
        self.request_releases(self.page_number + 1)

    def on_search_release(self, *_args):

        for iterator in self.releases_list_view.get_selected_rows():
            artist = self.releases_list_view.get_row_value(iterator, "artist")
            title = self.releases_list_view.get_row_value(iterator, "title")

            core.search.do_search(core.discogs.get_search_term(artist, title), mode="global")
            return
