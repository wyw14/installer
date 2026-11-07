"""Utilities related to handling / interacting with wheel files."""

import base64
import contextlib
import csv
import hashlib
import io
import os
import re
import sys
import time
from collections import namedtuple
from collections.abc import Callable, Iterable, Iterator
from configparser import ConfigParser
from email.message import Message
from email.parser import FeedParser
from email.policy import compat32
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    BinaryIO,
    NewType,
    cast,
)

from installer.exceptions import (
    InvalidBytecodeOptimizationLevel,
    InvalidSourceDateEpoch,
)

if TYPE_CHECKING:
    from installer.records import RecordEntry
    from installer.scripts import LauncherKind, ScriptSection

Scheme = NewType("Scheme", str)
AllSchemes = tuple[Scheme, ...]

# A date/time representable in the MS-DOS fields of a ZIP archive member, as
# six UTC values: (year, month, day, hour, minute, second). The seconds field
# only has a 2-second resolution.
ZipDateTime = tuple[int, int, int, int, int, int]

# Earliest and latest times representable in a ZIP archive's MS-DOS
# date/time fields, expressed as POSIX timestamps (UTC).
ZIP_DATE_TIME_MIN_EPOCH = 315_532_800  # 1980-01-01 00:00:00 UTC
ZIP_DATE_TIME_MAX_EPOCH = 4_354_819_198  # 2107-12-31 23:59:58 UTC
_ZIP_DATE_TIME_MIN: ZipDateTime = (1980, 1, 1, 0, 0, 0)
_ZIP_DATE_TIME_MAX: ZipDateTime = (2107, 12, 31, 23, 59, 58)

#: Fixed timestamp used for reproducible artifacts, when the caller does not
#: provide ``SOURCE_DATE_EPOCH``. This is 1980-01-01 00:00:00 UTC, the earliest
#: time that can be represented in a ZIP archive member's date/time fields.
DEFAULT_SOURCE_DATE_EPOCH: int = ZIP_DATE_TIME_MIN_EPOCH

__all__ = [
    "DEFAULT_SOURCE_DATE_EPOCH",
    "SCHEME_NAMES",
    "ZIP_DATE_TIME_MAX_EPOCH",
    "ZIP_DATE_TIME_MIN_EPOCH",
    "WheelFilename",
    "construct_record_file",
    "copyfileobj_with_hashing",
    "fix_shebang",
    "get_launcher_kind",
    "make_file_executable",
    "normalize_bytecode_optimization_levels",
    "parse_entrypoints",
    "parse_metadata_file",
    "parse_wheel_filename",
    "source_date_epoch_to_zip_date_time",
]

# Borrowed from https://github.com/python/cpython/blob/v3.9.1/Lib/shutil.py#L52
_WINDOWS = os.name == "nt"
_COPY_BUFSIZE = 1024 * 1024 if _WINDOWS else 64 * 1024

# According to https://www.python.org/dev/peps/pep-0427/#file-name-convention
_WHEEL_FILENAME_REGEX = re.compile(
    r"""
    ^
    (?P<distribution>.+?)
    -(?P<version>.*?)
    (?:-(?P<build_tag>\d[^-]*?))?
    -(?P<tag>.+?-.+?-.+?)
    \.whl
    $
    """,
    re.VERBOSE | re.UNICODE,
)
WheelFilename = namedtuple(
    "WheelFilename", ["distribution", "version", "build_tag", "tag"]
)

# Adapted from https://github.com/python/importlib_metadata/blob/v3.4.0/importlib_metadata/__init__.py#L90
_ENTRYPOINT_REGEX = re.compile(
    r"""
    (?P<module>[\w.]+)\s*
    (:\s*(?P<attrs>[\w.]+))\s*
    (?P<extras>\[.*\])?\s*$
    """,
    re.VERBOSE | re.UNICODE,
)

# According to https://www.python.org/dev/peps/pep-0427/#id7
SCHEME_NAMES = cast("AllSchemes", ("purelib", "platlib", "headers", "scripts", "data"))


def parse_metadata_file(contents: str) -> Message:
    """Parse :pep:`376` ``PKG-INFO``-style metadata files.

    ``METADATA`` and ``WHEEL`` files (as per :pep:`427`) use the same syntax
    and can also be parsed using this function.

    :param contents: The entire contents of the file
    """
    feed_parser = FeedParser(policy=compat32)
    feed_parser.feed(contents)
    return feed_parser.close()


def canonicalize_name(name: str) -> str:
    """Canonicalize a project name according to PEP-503.

    :param name: The project name to canonicalize
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_wheel_filename(filename: str) -> WheelFilename:
    """Parse a wheel filename, into it's various components.

    :param filename: The filename to parse
    """
    wheel_info = _WHEEL_FILENAME_REGEX.match(filename)
    if not wheel_info:
        raise ValueError(f"Not a valid wheel filename: {filename}")
    return WheelFilename(*wheel_info.groups())


def copyfileobj_with_hashing(
    source: BinaryIO,
    dest: BinaryIO,
    hash_algorithm: str,
) -> tuple[str, int]:
    """Copy a buffer while computing the content's hash and size.

    Copies the source buffer into the destination buffer while computing the
    hash of the contents. Adapted from :any:`shutil.copyfileobj`.

    :param source: buffer holding the source data
    :param dest: destination buffer
    :param hash_algorithm: hashing algorithm

    :return: hash digest of the contents, size of the contents
    """
    hasher = hashlib.new(hash_algorithm)
    size = 0
    while True:
        buf = source.read(_COPY_BUFSIZE)
        if not buf:
            break
        hasher.update(buf)
        dest.write(buf)
        size += len(buf)

    return base64.urlsafe_b64encode(hasher.digest()).decode("ascii").rstrip("="), size


def get_stream_length(source: BinaryIO) -> int:
    """Read a buffer while computing the content's size.

    :param source: buffer holding the source data
    :return: size of the contents
    """
    size = 0
    while True:
        buf = source.read(_COPY_BUFSIZE)
        if not buf:
            break
        size += len(buf)

    return size


def get_launcher_kind() -> "LauncherKind":  # pragma: no cover
    """Get the launcher kind for the current machine."""
    if os.name != "nt":
        return "posix"

    if "amd64" in sys.version.lower():
        return "win-amd64"
    if "(arm64)" in sys.version.lower():
        return "win-arm64"
    if "(arm)" in sys.version.lower():
        return "win-arm"
    if sys.platform == "win32":
        return "win-ia32"

    raise NotImplementedError("Unknown launcher kind for this machine")


@contextlib.contextmanager
def fix_shebang(stream: BinaryIO, interpreter: str) -> Iterator[BinaryIO]:
    """Replace ``#!python`` shebang in a stream with the correct interpreter.

    :param stream: stream to modify
    :param interpreter: "correct interpreter" to substitute the shebang with

    :returns: A context manager, that provides an appropriately modified stream.
    """
    stream.seek(0)
    if stream.read(8) == b"#!python":
        new_stream = io.BytesIO()
        # write our new shebang
        new_stream.write(f"#!{interpreter}\n".encode())
        # copy the rest of the stream
        stream.seek(0)
        stream.readline()  # skip first line
        while True:
            buf = stream.read(_COPY_BUFSIZE)
            if not buf:
                break
            new_stream.write(buf)
        new_stream.seek(0)
        yield new_stream
        new_stream.close()
    else:
        stream.seek(0)
        yield stream


def construct_record_file(
    records: Iterable[tuple[Scheme, "RecordEntry"]],
    prefix_for_scheme: Callable[[Scheme], str | None] = lambda _: None,
) -> BinaryIO:
    """Construct a RECORD file.

    :param records:
        ``records`` as passed into :any:`WheelDestination.finalize_installation`
    :param prefix_for_scheme:
        function to get a prefix to add for RECORD entries, within a scheme

    :return: A stream that can be written to file. Must be closed by the caller.
    """
    stream = io.TextIOWrapper(
        io.BytesIO(), encoding="utf-8", write_through=True, newline=""
    )
    writer = csv.writer(stream, delimiter=",", quotechar='"', lineterminator="\n")
    for scheme, record in sorted(records, key=lambda x: x[1].path):
        writer.writerow(record.to_row(prefix_for_scheme(scheme)))
    stream.seek(0)
    return stream.detach()


def parse_entrypoints(text: str) -> Iterable[tuple[str, str, str, "ScriptSection"]]:
    """Parse ``entry_points.txt``-style files.

    :param text: entire contents of the file
    :return:
        name of the script, module to use, attribute to call, kind of script (cli / gui)
    """
    # Borrowed from https://github.com/python/importlib_metadata/blob/v3.4.0/importlib_metadata/__init__.py#L115
    config = ConfigParser(delimiters="=")
    config.optionxform = str  # type: ignore[assignment, method-assign]
    config.read_string(text)

    for section in config.sections():
        if section not in ["console_scripts", "gui_scripts"]:
            continue

        for name, value in config.items(section):
            assert isinstance(name, str)
            match = _ENTRYPOINT_REGEX.match(value)
            assert match

            module = match.group("module")
            assert isinstance(module, str)

            attrs = match.group("attrs")
            # TODO: make this a proper error, which can be caught.
            assert attrs is not None
            assert isinstance(attrs, str)

            script_section = cast("ScriptSection", section[: -len("_scripts")])

            yield name, module, attrs, script_section


def _current_umask() -> int:
    """Get the current umask which involves having to set it temporarily."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


# Borrowed from:
# https://github.com/pypa/pip/blob/0f21fb92/src/pip/_internal/utils/unpacking.py#L93
def make_file_executable(path: Path) -> None:
    """Make the file at the provided path executable."""
    path.chmod(0o777 & ~_current_umask() | 0o111)


def normalize_bytecode_optimization_levels(
    levels: Iterable[object],
) -> tuple[int, ...]:
    """Validate and de-duplicate bytecode optimization levels.

    Only the levels supported by the Python compiler (``0``, ``1`` and ``2``)
    are accepted. The returned levels preserve the order of first occurrence.

    :param levels: optimization levels requested by the caller.
    :raises InvalidBytecodeOptimizationLevel: if any level is not one of
        ``0``, ``1`` or ``2``.
    """
    normalized: list[int] = []
    for level in levels:
        # ``bool`` is a subclass of ``int``; reject it explicitly, since
        # ``True``/``False`` are not valid optimization levels.
        if type(level) is not int or not (0 <= level <= 2):
            message = (
                f"Invalid bytecode optimization level {level!r}:"
                " expected one of 0, 1 or 2."
            )
            raise InvalidBytecodeOptimizationLevel(message)
        if level not in normalized:
            normalized.append(level)
    return tuple(normalized)


def source_date_epoch_to_zip_date_time(epoch: object) -> ZipDateTime:
    """Convert a ``SOURCE_DATE_EPOCH`` value to a ZIP member date/time.

    The value is interpreted in UTC and validated against the range of the
    MS-DOS date/time fields stored in ZIP archives (1980-01-01 00:00:00 UTC to
    2107-12-31 23:59:58 UTC).

    :param epoch: POSIX timestamp to use for reproducible archive members.
    :raises InvalidSourceDateEpoch: if the value is not an integer or cannot
        be represented in a ZIP archive member's date/time fields.
    """
    # ``bool`` is a subclass of ``int``; reject it explicitly.
    if type(epoch) is not int:
        message = f"SOURCE_DATE_EPOCH must be an integer, got {epoch!r}."
        raise InvalidSourceDateEpoch(message)

    try:
        gmtime = time.gmtime(epoch)
    except (OverflowError, OSError, ValueError) as exc:
        message = (
            f"SOURCE_DATE_EPOCH {epoch} is outside of the range of times"
            " representable in a ZIP archive (1980-01-01 00:00:00 UTC to"
            " 2107-12-31 23:59:58 UTC)."
        )
        raise InvalidSourceDateEpoch(message) from exc

    date_time: ZipDateTime = (
        gmtime.tm_year,
        gmtime.tm_mon,
        gmtime.tm_mday,
        gmtime.tm_hour,
        gmtime.tm_min,
        gmtime.tm_sec,
    )
    if not _ZIP_DATE_TIME_MIN <= date_time <= _ZIP_DATE_TIME_MAX:
        message = (
            f"SOURCE_DATE_EPOCH {epoch} ({time.strftime('%Y-%m-%d %H:%M:%S', gmtime)}"
            " UTC) is outside of the range of times representable in a ZIP"
            " archive (1980-01-01 00:00:00 UTC to 2107-12-31 23:59:58 UTC)."
        )
        raise InvalidSourceDateEpoch(message)
    return date_time
