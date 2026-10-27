# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import GObject

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.gtkgui.widgets import ui
from pynicotine.gtkgui.widgets.dialogs import Dialog
from pynicotine.gtkgui.widgets.treeview import TreeView


class DiscogsRelease(Dialog):
    """Search Discogs for the release in a folder about to be downloaded,
    so that the folder can be renamed and its files tagged afterwards."""

    def __init__(self, application, choose_callback):

        (
            self.cancel_button,
            self.choose_button,
            self.container,
            self.list_container,
            self.search_entry,
            self.status_label
        ) = ui.load(scope=self, path="dialogs/discogsrelease.ui")

        super().__init__(
            application=application,
            content_box=self.container,
            buttons_start=(self.cancel_button,),
            buttons_end=(self.choose_button,),
            default_button=self.choose_button,
            close_callback=self.on_close,
            title=_("Choose Discogs Release"),
            width=config.sections["discogs"]["release_dialog_width"],
            height=config.sections["discogs"]["release_dialog_height"],
            show_title_buttons=False
        )

        self.choose_callback = choose_callback

        # Only results for the latest search are shown, in case an older
        # request finishes after a newer one
        self.search_query = None
        self.release_groups = {}

        self.list_view = TreeView(
            application.window, parent=self.list_container,
            activate_row_callback=self.on_choose,
            select_row_callback=self.on_select_release,
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
                    "width": 160,
                    "expand_column": True
                },
                "title": {
                    "column_type": "text",
                    "title": _("Title"),
                    "width": 200,
                    "expand_column": True
                },
                "label": {
                    "column_type": "text",
                    "title": _("Label"),
                    "width": 140,
                    "expand_column": True
                },
                "catno": {
                    "column_type": "text",
                    "title": _("Catalogue Number"),
                    "width": 120
                },
                "format": {
                    "column_type": "text",
                    "title": _("Format"),
                    "width": 140,
                    "expand_column": True
                },

                # Hidden data columns
                "id_data": {"data_type": GObject.TYPE_UINT64, "iterator_key": True},
                "year_data": {"data_type": GObject.TYPE_UINT},
                "order_data": {"data_type": GObject.TYPE_UINT, "default_sort_type": "ascending"}
            }
        )

        events.connect("discogs-release-search", self.release_search)

    def destroy(self):
        self.list_view.destroy()
        super().destroy()

    def search(self, query):

        self.search_entry.set_text(query)
        self.list_view.clear()
        self.release_groups.clear()
        self.choose_button.set_sensitive(False)

        query = query.strip()

        if not query:
            self.search_query = None
            self.status_label.set_label(_("Type the artist, title or catalogue number to search for"))
            return

        self.search_query = query
        self.status_label.set_label(_("Searching…"))
        core.discogs.request_release_search(query)

    def release_search(self, query, release_groups, error_message):

        if query != self.search_query:
            return

        if error_message:
            self.status_label.set_label(
                _("Search failed: %s. A Discogs token, set in the Discogs tab, is needed to search.")
                % error_message)
            return

        if not release_groups:
            self.status_label.set_label(_("No releases found. Try fewer words, or the catalogue number."))
            return

        self.list_view.freeze()

        for order, group in enumerate(release_groups):
            self.release_groups[group.id] = group
            self.list_view.add_row([
                str(group.year or ""),
                group.artist,
                group.title,
                group.label,
                group.catno,
                group.release_format,
                group.id,
                group.year or 0,
                order
            ], select_row=False)

        self.list_view.unfreeze()
        self.status_label.set_label(
            _("Choose the release in the folder. Once downloaded, the folder is renamed and its files are tagged."))

    def get_selected_release_group(self):

        for iterator in self.list_view.get_selected_rows():
            return self.release_groups.get(self.list_view.get_row_value(iterator, "id_data"))

        return None

    def on_select_release(self, *_args):
        self.choose_button.set_sensitive(self.get_selected_release_group() is not None)

    def on_search(self, *_args):
        self.search(self.search_entry.get_text())

    def on_choose(self, *_args):

        group = self.get_selected_release_group()

        if group is None:
            return

        self.choose_callback(group)
        self.close()

    def _resize_dialog(self):

        # Dialogs are shrunk to fit their parent, but this one can be larger
        # than the download dialog it's opened from. Only fit the main window.
        parent = self.parent
        self.parent = self.application.window

        super()._resize_dialog()
        self.parent = parent

    def on_cancel(self, *_args):
        self.close()

    def on_close(self, *_args):

        width = self.get_width()
        height = self.get_height()

        if width <= 0 or height <= 0:
            return

        # Remember the size for the next time the dialog is shown
        self.default_width = width
        self.default_height = height
        config.sections["discogs"]["release_dialog_width"] = width
        config.sections["discogs"]["release_dialog_height"] = height
