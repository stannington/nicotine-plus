# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Run from the repository root (these match CI in `.github/workflows/tests.yml`):

```sh
./nicotine                          # run from the source tree (GTK GUI)
./nicotine --headless               # run without a GUI
python3 -m unittest -v              # all unit + integration tests
python3 -m pycodestyle              # style check (max line length 120)
python3 -m pylint --recursive=y .   # lint (config in pyproject.toml)
python3 -m build                    # build sdist + wheel
reuse lint                          # SPDX license header check (CI: reuse.yml)
```

Run a single test module / class / method:

```sh
python3 -m unittest pynicotine.tests.unit.transfers.test_uploads
python3 -m unittest pynicotine.tests.unit.transfers.test_uploads.UploadsTest.test_load_uploads
```

After changing translatable strings (`_()` / `ngettext()`), regenerate the template with
`python3 data/scripts/update_translation_template.py`. Don't edit `po/*.po` files by hand; Weblate manages them.

## Architecture

Pure Python (minimum 3.10) with GTK 3 or 4 through PyGObject. Prefer the standard library over new dependencies.
Small third-party modules are vendored in `pynicotine/external/`.

Three layers, all under `pynicotine/`:

1. **Network**: `slskproto.py` runs `NetworkThread`, a selector-based socket loop on its own thread that handles the
   server, peer and file-transfer connections. `slskmessages.py` defines every Soulseek protocol message as a class
   that knows how to serialize and parse itself. `doc/SLSKPROTOCOL.md` documents the protocol.
2. **Core**: the feature modules at the top level of `pynicotine/` (`search.py`, `downloads.py`, `uploads.py`,
   `transfers.py`, `shares.py`, `chatrooms.py`, `privatechat.py`, `users.py`, and so on). `core.py` exposes a global
   `core` singleton, so other code reaches them as `core.search`, `core.downloads`, etc.
   `core.init_components(enabled_components=...)` selects which ones to create.
3. **UI**: `gtkgui/` (GTK; layouts live in `gtkgui/ui/*.ui` XML files, with Python controllers alongside) or
   `headless/`. `pynicotine/__init__.py:run()` parses the CLI flags and picks which one to start.

### Event bus (`events.py`): how the layers communicate

- Everything goes through the global `events` object: `events.connect(name, callback)` and `events.emit(name, ...)`.
  Every event name must be listed in the `EVENT_NAMES` set at the top of `events.py`.
- **Core never imports the GUI.** The GUI calls `core.*` methods directly, and core notifies the GUI by emitting
  events. Keep this one-way dependency, because headless mode relies on it.
- Outgoing network traffic: core calls `core.send_message_to_server(msg)` or `core.send_message_to_peer(user, msg)`,
  which emit `queue-network-message` for the network thread to pick up.
- Incoming network traffic: `NetworkThread` parses each message and looks up its class in `NETWORK_MESSAGE_EVENTS`
  (`slskmessages.py`, around line 4239). It then calls `events.emit_main_thread(event_name, msg)`, and the core
  modules subscribe to those event names. Adding a new handled message means adding it to that mapping.
- Code running on any thread other than the main one must use `events.emit_main_thread` or
  `events.invoke_main_thread`, never touch core or GUI state directly. Use `events.schedule()` for timers.

### Other cross-cutting pieces

- `config.py`: global `config`; settings are read as `config.sections["section"]["key"]`.
- `pluginsystem.py` loads the plugins in `plugins/`. Each plugin is a folder with a `PLUGININFO` file and an
  `__init__.py`; see `plugins/README.md`.
- Tests use `unittest` and live in `pynicotine/tests/{unit,integration}`. Unit tests point `config` at a temporary
  data folder, call `core.init_components()` with only the components they need, then `core.start()` and
  `core.quit()` in `setUp` and `tearDown`.

## Conventions

- Every file starts with SPDX headers (`SPDX-FileCopyrightText` / `SPDX-License-Identifier: GPL-3.0-or-later`);
  CI enforces this with `reuse lint`.
- Contributor rules are in `CONTRIBUTING.md`; design and release policy is in `doc/DEVELOPING.md`.
- Verbose debug logging categories (connections, messages, transfers) can be enabled from the log pane's context menu.
