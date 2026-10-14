# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from pynicotine.gtkgui.application import GTK_API_VERSION
from pynicotine.gtkgui.widgets import ui


class Discogs:

    def __init__(self, window):

        (
            self.container,
            self.placeholder_label
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

    def destroy(self):
        self.__dict__.clear()

    def on_focus(self, *_args):
        return False
