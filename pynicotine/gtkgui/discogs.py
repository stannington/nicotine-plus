# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Pango

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.gtkgui.application import GTK_API_VERSION
from pynicotine.gtkgui.widgets import ui
from pynicotine.gtkgui.widgets.dialogs import EntryDialog
from pynicotine.gtkgui.widgets.filechooser import FolderChooser
from pynicotine.gtkgui.widgets.popupmenu import PopupMenu
from pynicotine.gtkgui.widgets.treeview import TreeView
from pynicotine.utils import humanize


class Discogs:

    def __init__(self, window):

        (
            self.container,
            self.folder_name_button,
            self.hide_owned_toggle,
            self.labels_list_container,
            self.library_folders_button,
            self.library_label,
            self.load_more_button,
            self.releases_label,
            self.releases_list_container,
            self.rescan_library_button,
            self.search_entry,
            self.status_label,
            self.token_button
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

        # All loaded releases of the selected label, including hidden ones
        self.releases = {}
        self.last_seen_release_id = None

        self.labels_list_view = TreeView(
            window, parent=self.labels_list_container,
            select_row_callback=self.on_select_label,
            columns={
                # Visible columns
                "favourite": {
                    "column_type": "icon",
                    "title": _("Favourite"),
                    "width": 25,
                    "hide_header": True
                },
                "label": {
                    "column_type": "text",
                    "title": _("Label")
                },

                # Hidden data columns
                "id_data": {"data_type": GObject.TYPE_UINT64, "iterator_key": True},
                "order_data": {"data_type": GObject.TYPE_UINT, "default_sort_type": "ascending"}
            }
        )

        self.labels_popup_menu = PopupMenu(
            self.window.application, self.labels_list_view.widget, callback=self.on_labels_popup_menu)
        self.labels_popup_menu.add_items(
            ("$" + _("_Favourite Label"), self.on_favourite_label)
        )

        self.releases_list_view = TreeView(
            window, parent=self.releases_list_container,
            activate_row_callback=self.on_search_release,
            columns={
                # Visible columns
                "in_library": {
                    "column_type": "icon",
                    "title": _("In Library"),
                    "width": 25,
                    "hide_header": True,
                    "tooltip_callback": self.on_library_tooltip
                },
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
                    "expand_column": True,
                    "text_weight_column": "weight_data"
                },
                "title": {
                    "column_type": "text",
                    "title": _("Title"),
                    "width": 250,
                    "expand_column": True,
                    "text_weight_column": "weight_data"
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
                "order_data": {"data_type": GObject.TYPE_UINT, "default_sort_type": "ascending"},
                "library_path_data": {"data_type": GObject.TYPE_STRING},
                "weight_data": {"data_type": Pango.Weight}
            }
        )

        self.popup_menu = PopupMenu(self.window.application, self.releases_list_view.widget)
        self.popup_menu.add_items(
            ("#" + _("_Search for Release"), self.on_search_release),
            ("#" + _("Search for _Catalogue Number"), self.on_search_catno)
        )

        self.hide_owned_toggle.set_active(config.sections["discogs"]["hide_owned_releases"])
        self.show_favourite_labels()

        for event_name, callback in (
            ("discogs-label-releases", self.label_releases),
            ("discogs-label-search", self.label_search),
            ("discogs-library-updated", self.library_updated)
        ):
            events.connect(event_name, callback)

    def destroy(self):

        self.popup_menu.destroy()
        self.labels_popup_menu.destroy()
        self.labels_list_view.destroy()
        self.releases_list_view.destroy()

        self.__dict__.clear()

    def on_focus(self, *_args):

        self.scan_library_if_needed()
        self.search_entry.grab_focus()
        return True

    def scan_library_if_needed(self):

        # Scan when the tab is first used, rather than at startup, since it can
        # take a while
        if core.discogs.library is None and not core.discogs.is_scanning_library:
            self.scan_library()

    def scan_library(self):
        core.discogs.request_library_scan()
        self.update_library_status()

    def update_library_status(self):

        library = core.discogs.library
        is_scanning = core.discogs.is_scanning_library

        if library is None:
            status = _("Scanning library…") if is_scanning else _("Library not scanned")

        elif is_scanning:
            # Showing results from the previous scan until this one is done
            status = _("Library: %(num_folders)s folders, updating…") % {
                "num_folders": humanize(len(library.folder_paths))}

        else:
            status = _("Library: %(num_folders)s folders") % {"num_folders": humanize(len(library.folder_paths))}

        self.library_label.set_label(status)
        self.library_label.set_tooltip_text("\n".join(core.discogs.get_library_folders()))
        self.rescan_library_button.set_sensitive(not core.discogs.is_scanning_library)

    @staticmethod
    def get_library_state(release):
        """Return the library folder of a release, and whether it's likely
        an earlier version of the release."""

        library_path = core.discogs.find_in_library(release.artist, release.title, release.catno) or ""
        is_earlier_version = bool(library_path) and core.discogs.is_earlier_version(library_path, release.year)

        return library_path, is_earlier_version

    def is_release_hidden(self, release):

        if not self.hide_owned_toggle.get_active():
            return False

        library_path, is_earlier_version = self.get_library_state(release)
        return bool(library_path) and not is_earlier_version

    def add_release_row(self, release, order):

        library_path, is_earlier_version = self.get_library_state(release)

        if not library_path:
            icon_name = ""
        elif is_earlier_version:
            icon_name = "document-open-recent-symbolic"
        else:
            icon_name = "object-select-symbolic"

        is_new = self.last_seen_release_id is not None and release.id > self.last_seen_release_id

        self.releases_list_view.add_row([
            icon_name,
            str(release.year or ""),
            release.artist,
            release.title,
            release.catno,
            release.release_format,
            release.id,
            release.year or 0,
            order,
            library_path,
            Pango.Weight.BOLD if is_new else Pango.Weight.NORMAL
        ], select_row=False)

    def show_releases(self):
        """Fill the release list again, e.g. after the library changed."""

        self.releases_list_view.clear()
        self.releases_list_view.freeze()

        for order, release in enumerate(self.releases.values()):
            if not self.is_release_hidden(release):
                self.add_release_row(release, order)

        self.releases_list_view.unfreeze()

    def library_updated(self):

        self.update_library_status()

        if self.releases:
            self.show_releases()
            self.update_status()

    def request_releases(self, page_number):

        self.page_number = page_number
        self.load_more_button.set_sensitive(False)
        self.status_label.set_label(_("Loading…"))

        core.discogs.request_label_releases(self.label_id, page_number)

    def update_status(self):

        num_releases = len(self.releases)
        num_in_library = num_earlier_versions = num_new = 0

        for release in self.releases.values():
            library_path, is_earlier_version = self.get_library_state(release)

            if is_earlier_version:
                num_earlier_versions += 1

            elif library_path:
                num_in_library += 1

            if self.last_seen_release_id is not None and release.id > self.last_seen_release_id:
                num_new += 1

        has_more_pages = self.page_number < self.num_pages

        if not num_releases:
            status = _("No releases found")

        elif has_more_pages:
            status = _("Showing the %(num_releases)s newest releases") % {"num_releases": humanize(num_releases)}

        else:
            status = _("Showing all %(num_releases)s releases") % {"num_releases": humanize(num_releases)}

        if num_in_library:
            status += ", " + _("%(num_releases)s in your library") % {"num_releases": humanize(num_in_library)}

            if self.hide_owned_toggle.get_active():
                status += " " + _("(hidden)")

        if num_earlier_versions:
            status += ", " + _("%(num_releases)s with an earlier version in your library") % {
                "num_releases": humanize(num_earlier_versions)}

        if num_new:
            status += ", " + _("%(num_releases)s new since you last looked") % {"num_releases": humanize(num_new)}

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
            self.add_label_row(label.id, label.name, order)

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
        self.releases_list_view.freeze()

        for release in releases:
            if release.id in self.releases:
                # Also on an earlier page
                continue

            order = len(self.releases)
            self.releases[release.id] = release

            if not self.is_release_hidden(release):
                self.add_release_row(release, order)

        self.releases_list_view.unfreeze()

        if releases:
            core.discogs.set_last_seen_release_id(label_id, max(release.id for release in releases))
            config.write_configuration()

        self.update_status()

    def on_search_labels(self, *_args):

        query = self.search_entry.get_text().strip()

        if not query:
            self.show_favourite_labels()
            return

        self.search_query = query
        self.clear_labels()
        self.status_label.set_label(_("Searching…"))

        core.discogs.request_label_search(query)
        self.scan_library_if_needed()

    def on_select_label(self, list_view, iterator):

        if iterator is None:
            return

        label_id = list_view.get_row_value(iterator, "id_data")

        if label_id == self.label_id:
            return

        self.label_id = label_id
        self.num_pages = 0
        self.releases.clear()
        self.last_seen_release_id = core.discogs.get_last_seen_release_id(label_id)

        self.releases_list_view.clear()
        self.releases_label.set_label(list_view.get_row_value(iterator, "label"))
        self.load_more_button.set_visible(False)
        self.request_releases(page_number=1)

    def clear_labels(self):

        self.label_id = None
        self.releases.clear()
        self.labels_list_view.clear()
        self.releases_list_view.clear()
        self.releases_label.set_label(_("Releases"))
        self.load_more_button.set_visible(False)

    def add_label_row(self, label_id, name, order):

        icon_name = "starred-symbolic" if core.discogs.is_favourite_label(label_id) else ""
        self.labels_list_view.add_row([icon_name, name, label_id, order], select_row=False)

    def show_favourite_labels(self):

        self.search_query = None
        self.clear_labels()

        favourite_labels = config.sections["discogs"]["favourite_labels"]

        for order, (label_id, name) in enumerate(favourite_labels):
            self.add_label_row(label_id, name, order)

        if favourite_labels:
            self.status_label.set_label(_("Select a favourite label, or search for a label"))
        else:
            self.status_label.set_label(_("Search for a label, then select it to see its releases"))

    def get_selected_label(self):

        for iterator in self.labels_list_view.get_selected_rows():
            return iterator

        return None

    def on_labels_popup_menu(self, menu, _widget):

        iterator = self.get_selected_label()
        label_id = self.labels_list_view.get_row_value(iterator, "id_data")

        menu.actions[_("_Favourite Label")].set_state(
            GLib.Variant.new_boolean(core.discogs.is_favourite_label(label_id)))

    def on_favourite_label(self, action, state):

        iterator = self.get_selected_label()

        if iterator is None:
            return

        label_id = self.labels_list_view.get_row_value(iterator, "id_data")

        if state.get_boolean():
            core.discogs.add_favourite_label(label_id, self.labels_list_view.get_row_value(iterator, "label"))
            self.labels_list_view.set_row_value(iterator, "favourite", "starred-symbolic")

        else:
            core.discogs.remove_favourite_label(label_id)
            self.labels_list_view.set_row_value(iterator, "favourite", "")

        action.set_state(state)

    def on_hide_owned_toggled(self, *_args):

        config.sections["discogs"]["hide_owned_releases"] = self.hide_owned_toggle.get_active()

        if self.releases:
            self.show_releases()
            self.update_status()

    def on_load_more(self, *_args):
        self.request_releases(self.page_number + 1)

    def on_library_tooltip(self, list_view, iterator):

        library_path = list_view.get_row_value(iterator, "library_path_data")

        if not library_path:
            return None

        if list_view.get_row_value(iterator, "in_library") == "document-open-recent-symbolic":
            return _("Earlier version in library: %(path)s") % {"path": library_path}

        return _("In library: %(path)s") % {"path": library_path}

    def on_choose_library_folders(self, *_args):

        library_folders = config.sections["discogs"]["library_folders"]

        FolderChooser(
            application=self.window.application,
            title=_("Select Library Folders"),
            callback=self.on_library_folders_selected,
            initial_folder=library_folders[0] if library_folders else None,
            select_multiple=True
        ).present()

    def on_library_folders_selected(self, selected_folder_paths, _data):

        config.sections["discogs"]["library_folders"] = list(selected_folder_paths)
        config.write_configuration()

        self.scan_library()

    def on_rescan_library(self, *_args):
        self.scan_library()

    def on_set_token(self, *_args):

        EntryDialog(
            application=self.window.application,
            title=_("Discogs Personal Access Token"),
            message=_("A token lets you make more Discogs requests per minute. Generate one in your Discogs "
                      "account under Settings → Developers. Leave empty to remove the token."),
            default=config.sections["discogs"]["token"],
            visibility=False,
            action_button_label=_("_Save"),
            callback=self.on_token_response
        ).present()

    def on_token_response(self, dialog, _response_id, _data):

        if not self.__dict__:
            # Tab was closed
            return

        config.sections["discogs"]["token"] = dialog.get_entry_value().strip()
        config.write_configuration()

    def on_set_folder_name(self, *_args):

        EntryDialog(
            application=self.window.application,
            title=_("Downloaded Folder Name"),
            message=_("Folders of releases you search for from this tab are renamed once downloaded. "
                      "Available variables: $year, $catno, $artist and $title. "
                      "Leave empty to keep the original folder names."),
            default=config.sections["discogs"]["folder_name_pattern"],
            action_button_label=_("_Save"),
            callback=self.on_folder_name_response
        ).present()

    def on_folder_name_response(self, dialog, _response_id, _data):

        if not self.__dict__:
            # Tab was closed
            return

        config.sections["discogs"]["folder_name_pattern"] = dialog.get_entry_value().strip()
        config.write_configuration()

    def on_search_release(self, *_args):

        for iterator in self.releases_list_view.get_selected_rows():
            artist = self.releases_list_view.get_row_value(iterator, "artist")
            title = self.releases_list_view.get_row_value(iterator, "title")
            year = self.releases_list_view.get_row_value(iterator, "year_data")
            catno = self.releases_list_view.get_row_value(iterator, "catno")

            core.discogs.add_pending_release(artist, title, year, catno)
            core.search.do_search(core.discogs.get_search_term(artist, title), mode="global")
            return

    def on_search_catno(self, *_args):

        for iterator in self.releases_list_view.get_selected_rows():
            artist = self.releases_list_view.get_row_value(iterator, "artist")
            title = self.releases_list_view.get_row_value(iterator, "title")
            year = self.releases_list_view.get_row_value(iterator, "year_data")
            catno = self.releases_list_view.get_row_value(iterator, "catno")
            search_term = core.discogs.get_catno_search_term(catno)

            if not search_term:
                self.status_label.set_label(_("This release has no catalogue number"))
                return

            core.discogs.add_pending_release(artist, title, year, catno)
            core.search.do_search(search_term, mode="global")
            return
