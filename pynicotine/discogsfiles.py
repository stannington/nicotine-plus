# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Renaming and tagging the audio files of a release downloaded from the
Discogs tab, using the release's tracklist on Discogs."""

import os
import re
import string
import unicodedata

from pynicotine.logfacility import log
from pynicotine.utils import clean_file
from pynicotine.utils import encode_path

try:
    from mutagen import File as MutagenFile
    from mutagen import MutagenError
    from mutagen.flac import FLAC
    from mutagen.flac import Picture
    from mutagen.id3 import APIC
    from mutagen.id3 import ID3
    from mutagen.id3 import ID3NoHeaderError
    from mutagen.id3 import TALB
    from mutagen.id3 import TCON
    from mutagen.id3 import TDRC
    from mutagen.id3 import TIT2
    from mutagen.id3 import TPE1
    from mutagen.id3 import TPE2
    from mutagen.id3 import TPUB
    from mutagen.id3 import TRCK
    from mutagen.id3 import TXXX
    HAS_MUTAGEN = True

except ImportError:
    HAS_MUTAGEN = False

AUDIO_EXTENSIONS = {".flac", ".mp3"}
COVER_FILE_NAMES = {"cover", "folder", "front"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}

ARTIST_NUMBER_PATTERN = re.compile(r"\s*\(\d+\)")  # "Calibre (2)"
WORD_PATTERN = re.compile(r"[^\W_]+")
NUMBER_PATTERN = re.compile(r"(\d+)")


class DiscogsTrack:
    __slots__ = ("number", "artist", "title")

    def __init__(self, number, artist, title):
        self.number = number
        self.artist = artist
        self.title = title


class ReleaseTags:
    """Tags shared by all tracks of a release."""

    __slots__ = ("release_id", "artist", "album", "year", "label", "catno", "genres", "styles", "num_tracks")

    def __init__(self, release_id, artist, album, year, label, catno, genres, styles, num_tracks):
        self.release_id = release_id
        self.artist = artist
        self.album = album
        self.year = year
        self.label = label
        self.catno = catno
        self.genres = genres
        self.styles = styles
        self.num_tracks = num_tracks


def get_artist_name(artists):
    """Join Discogs artists as credited, e.g. "Seba & Paradox"."""

    parts = []

    for index, artist in enumerate(artists):
        parts.append(ARTIST_NUMBER_PATTERN.sub("", artist.get("anv") or artist.get("name", "")).replace("*", ""))

        if index < len(artists) - 1:
            join = artist.get("join", "").strip()
            parts.append(", " if join in {"", ","} else f" {join} ")

    return " ".join("".join(parts).split())


def get_tracks(release_data):
    """Return the tracks of a release in order, numbered from 1. Headings,
    e.g. "Side A", are not tracks."""

    release_artist = get_artist_name(release_data.get("artists", []))
    tracks = []

    for item in release_data.get("tracklist", []):
        if item.get("type_", "track") not in {"track", "index"} or not item.get("title"):
            continue

        artist = get_artist_name(item.get("artists", [])) or release_artist
        tracks.append(DiscogsTrack(len(tracks) + 1, artist, item["title"].strip()))

    return tracks


def get_release_tags(release_data, num_tracks):

    labels = release_data.get("labels", [])
    catno = labels[0].get("catno", "") if labels else ""

    return ReleaseTags(
        release_id=release_data.get("id"),
        artist=get_artist_name(release_data.get("artists", [])),
        album=release_data.get("title", "").strip(),
        year=release_data.get("year") or None,
        label=ARTIST_NUMBER_PATTERN.sub("", labels[0].get("name", "")) if labels else "",
        catno="" if catno.lower() == "none" else catno,
        genres=release_data.get("genres", []),
        styles=release_data.get("styles", []),
        num_tracks=num_tracks
    )


def get_cover_url(release_data):
    """Return the URL of the release's main image, or None."""

    images = release_data.get("images", [])

    for image in images:
        if image.get("type") == "primary":
            return image.get("uri")

    return images[0].get("uri") if images else None


def get_words(text):
    """Words in a name, ignoring accents and apostrophes, so that "Don't"
    matches "dont"."""

    text = unicodedata.normalize("NFKD", text.lower().replace("'", "").replace("’", ""))
    return set(WORD_PATTERN.findall("".join(char for char in text if not unicodedata.combining(char))))


def get_natural_sort_key(text):
    """Sort "2 b" before "10 a"."""
    return [int(part) if part.isdigit() else part for part in NUMBER_PATTERN.split(text.lower())]


class AudioFile:
    __slots__ = ("path", "name", "words", "track_number")

    def __init__(self, path, title_tag=None, track_number=None):
        self.path = path
        self.name = os.path.basename(path)
        self.words = get_words(os.path.splitext(self.name)[0])
        self.track_number = track_number

        if title_tag:
            self.words |= get_words(title_tag)


def read_audio_file(file_path):
    """Read the title and track number tags of a file, if possible."""

    title_tag = track_number = None

    if HAS_MUTAGEN:
        try:
            audio = MutagenFile(encode_path(file_path), easy=True)

            if audio is not None and audio.tags is not None:
                title_tag = " ".join(audio.tags.get("title", []))
                track_number = int(audio.tags.get("tracknumber", [""])[0].split("/")[0])

        except (MutagenError, OSError, ValueError, IndexError):
            pass

    return AudioFile(file_path, title_tag, track_number)


def match_files(audio_files, tracks):
    """Return a dict of audio file -> track. Files that can't be matched
    confidently are left out.

    If there are as many files as tracks, the files are matched in order,
    as long as no file's title points to a different track. Otherwise, each
    file is matched to the track whose title words are all in the file's
    name or title tag, taking the longest title, e.g. "Feet (Dub Mix)"
    rather than "Feet".
    """

    title_matches = {}
    track_words = [get_words(track.title) for track in tracks]

    for audio_file in audio_files:
        candidates = [index for index, words in enumerate(track_words) if words and words <= audio_file.words]

        if not candidates:
            continue

        longest = max(len(track_words[index]) for index in candidates)
        best_candidates = [index for index in candidates if len(track_words[index]) == longest]

        if len(best_candidates) == 1:
            title_matches[audio_file] = best_candidates[0]

    if len(audio_files) == len(tracks):
        track_numbers = [audio_file.track_number for audio_file in audio_files]

        if None not in track_numbers and sorted(track_numbers) == list(range(1, len(tracks) + 1)):
            ordered_files = sorted(audio_files, key=lambda audio_file: audio_file.track_number)
        else:
            ordered_files = sorted(audio_files, key=lambda audio_file: get_natural_sort_key(audio_file.name))

        if all(title_matches.get(audio_file, index) == index for index, audio_file in enumerate(ordered_files)):
            return {audio_file: tracks[index] for index, audio_file in enumerate(ordered_files)}

    # Leave out tracks matched by more than one file
    num_matches = {}

    for index in title_matches.values():
        num_matches[index] = num_matches.get(index, 0) + 1

    return {
        audio_file: tracks[index] for audio_file, index in title_matches.items() if num_matches[index] == 1
    }


def get_file_name(pattern, track, num_tracks, extension):
    """Create a file name for a track from a pattern such as
    "$track $artist - $title"."""

    track_number = str(track.number).zfill(max(2, len(str(num_tracks))))
    file_name = string.Template(pattern).safe_substitute(track=track_number, artist=track.artist, title=track.title)

    return clean_file(" ".join(file_name.split()) + extension)


def write_flac_tags(file_path, release_tags, track, cover):

    audio = FLAC(encode_path(file_path))

    if audio.tags is None:
        audio.add_tags()

    for key, values in (
        ("ARTIST", [track.artist]),
        ("ALBUMARTIST", [release_tags.artist]),
        ("TITLE", [track.title]),
        ("ALBUM", [release_tags.album]),
        ("TRACKNUMBER", [str(track.number)]),
        ("TRACKTOTAL", [str(release_tags.num_tracks)]),
        ("TOTALTRACKS", [str(release_tags.num_tracks)]),
        ("DATE", [str(release_tags.year)] if release_tags.year else []),
        ("LABEL", [release_tags.label]),
        ("CATALOGNUMBER", [release_tags.catno]),
        ("GENRE", release_tags.genres),
        ("STYLE", release_tags.styles),
        ("DISCOGS_RELEASE_ID", [str(release_tags.release_id)])
    ):
        values = [value for value in values if value]

        if values:
            audio.tags[key] = values

        elif key in audio.tags:
            # Replace old values, even if Discogs has none
            del audio.tags[key]

    if cover is not None:
        cover_data, mime_type = cover
        picture = Picture()
        picture.type = 3  # Front cover
        picture.mime = mime_type
        picture.data = cover_data

        audio.clear_pictures()
        audio.add_picture(picture)

    audio.save()


def write_mp3_tags(file_path, release_tags, track, cover):

    try:
        tags = ID3(encode_path(file_path))

    except ID3NoHeaderError:
        tags = ID3()

    for frame_id, frame_class, values in (
        ("TPE1", TPE1, [track.artist]),
        ("TPE2", TPE2, [release_tags.artist]),
        ("TIT2", TIT2, [track.title]),
        ("TALB", TALB, [release_tags.album]),
        ("TRCK", TRCK, [f"{track.number}/{release_tags.num_tracks}"]),
        ("TDRC", TDRC, [str(release_tags.year)] if release_tags.year else []),
        ("TPUB", TPUB, [release_tags.label]),
        ("TCON", TCON, release_tags.genres)
    ):
        values = [value for value in values if value]
        tags.delall(frame_id)

        if values:
            tags.add(frame_class(encoding=3, text=values))

    for description, values in (
        ("CATALOGNUMBER", [release_tags.catno]),
        ("STYLE", release_tags.styles),
        ("DISCOGS_RELEASE_ID", [str(release_tags.release_id)])
    ):
        values = [value for value in values if value]
        tags.delall(f"TXXX:{description}")

        if values:
            tags.add(TXXX(encoding=3, desc=description, text=values))

    if cover is not None:
        cover_data, mime_type = cover
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime_type, type=3, desc="Cover", data=cover_data))

    # ID3v2.3, which more players support than v2.4
    tags.save(encode_path(file_path), v2_version=3, v23_sep="; ")


def has_cover_file(file_names):
    return any(
        os.path.splitext(file_name)[0].lower() in COVER_FILE_NAMES
        and os.path.splitext(file_name)[1].lower() in IMAGE_EXTENSIONS
        for file_name in file_names
    )


def save_cover_file(folder_path, file_names, cover):
    """Save the cover as cover.jpg, unless the folder already has a cover
    image. Return True if saved."""

    if cover is None or has_cover_file(file_names):
        return False

    cover_data, mime_type = cover
    extension = ".png" if mime_type == "image/png" else ".jpg"

    with open(encode_path(os.path.join(folder_path, "cover" + extension)), "xb") as file_handle:
        file_handle.write(cover_data)

    return True


class FolderResult:
    """What was done to a downloaded folder, for the log."""

    __slots__ = ("num_files", "num_tagged", "num_renamed", "unmatched_file_names", "errors", "saved_cover")

    def __init__(self):
        self.num_files = 0
        self.num_tagged = 0
        self.num_renamed = 0
        self.unmatched_file_names = []
        self.errors = []
        self.saved_cover = False


def process_folder(folder_path, release_data, cover, file_name_pattern, should_tag):
    """Tag and rename the audio files directly in a downloaded release
    folder, and save its cover. The cover is a tuple of image data and mime
    type, or None. Returns a FolderResult."""

    result = FolderResult()

    try:
        file_names = []

        for file_name in os.listdir(encode_path(folder_path)):
            if isinstance(file_name, bytes):
                file_name = file_name.decode("utf-8", "replace")

            file_names.append(file_name)

    except OSError as error:
        result.errors.append(str(error))
        return result

    audio_files = [
        read_audio_file(os.path.join(folder_path, file_name)) for file_name in sorted(file_names)
        if os.path.splitext(file_name)[1].lower() in AUDIO_EXTENSIONS
    ]
    result.num_files = len(audio_files)

    tracks = get_tracks(release_data)
    release_tags = get_release_tags(release_data, len(tracks))
    matches = match_files(audio_files, tracks)
    should_tag = should_tag and HAS_MUTAGEN

    for audio_file in audio_files:
        track = matches.get(audio_file)

        if track is None:
            result.unmatched_file_names.append(audio_file.name)
            continue

        file_path = audio_file.path
        extension = os.path.splitext(file_path)[1].lower()

        if should_tag:
            try:
                if extension == ".flac":
                    write_flac_tags(file_path, release_tags, track, cover)
                else:
                    write_mp3_tags(file_path, release_tags, track, cover)

                result.num_tagged += 1

            except (MutagenError, OSError, ValueError) as error:
                log.add_debug("Unable to tag %s: %s", (file_path, error))
                result.errors.append(f"{audio_file.name}: {error}")

        if not file_name_pattern:
            continue

        new_file_name = get_file_name(file_name_pattern, track, len(tracks), extension)
        new_file_path = os.path.join(folder_path, new_file_name)

        if new_file_path == file_path:
            continue

        if os.path.exists(encode_path(new_file_path)):
            result.errors.append(f"{audio_file.name}: {new_file_name} already exists")
            continue

        try:
            os.rename(encode_path(file_path), encode_path(new_file_path))
            result.num_renamed += 1

        except OSError as error:
            result.errors.append(f"{audio_file.name}: {error}")

    try:
        result.saved_cover = save_cover_file(folder_path, file_names, cover)

    except OSError as error:
        result.errors.append(f"cover: {error}")

    return result
