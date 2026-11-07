import io
import stat
import zipfile
from pathlib import Path

import pytest

from installer import _scripts
from installer.exceptions import InvalidSourceDateEpoch
from installer.scripts import InvalidScript, Script


def _embedded_zip_info(data):
    """Parse the ZIP archive embedded at the end of a Windows launcher."""
    zip_start = data.index(b"PK\x03\x04")
    with zipfile.ZipFile(io.BytesIO(data[zip_start:])) as zf:
        return zf.infolist()


def test_script_generate_simple():
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    name, data = script.generate("/path/to/my/python", kind="posix")

    assert name == "foo"
    assert data.startswith(b"#!/path/to/my/python\n")
    assert b"\nfrom foo.bar import baz\n" in data
    assert b"baz.qux()" in data


def test_script_generate_space_in_executable():
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    name, data = script.generate("/path to my/python", kind="posix")

    assert name == "foo"
    assert data.startswith(b"#!/bin/sh\n")
    assert b" '/path to my/python'" in data
    assert b"\nfrom foo.bar import baz\n" in data
    assert b"baz.qux()" in data


def _read_launcher_data(section, kind):
    prefix = {"console": "t", "gui": "w"}[section]
    suffix = {"win-ia32": "32", "win-amd64": "64", "win-arm": "_arm"}[kind]
    file = Path(_scripts.__file__).parent / f"{prefix}{suffix}.exe"
    return file.read_bytes()


@pytest.mark.parametrize("section", ["console", "gui"])
@pytest.mark.parametrize("kind", ["win-ia32", "win-amd64", "win-arm"])
def test_script_generate_launcher(section, kind):
    launcher_data = _read_launcher_data(section, kind)

    script = Script("foo", "foo.bar", "baz.qux", section=section)
    name, data = script.generate("#!C:\\path to my\\python.exe\n", kind=kind)

    prefix_len = len(launcher_data) + len(b"#!C:\\path to my\\python.exe\n")
    stream = io.BytesIO(data[prefix_len:])
    with zipfile.ZipFile(stream) as zf:
        code = zf.read("__main__.py")

    assert name == "foo.exe"
    assert data.startswith(launcher_data)
    if section == "gui":
        assert b"#!C:\\path to my\\pythonw.exe\n" in data
    else:
        assert b"#!C:\\path to my\\python.exe\n" in data
    assert b"\nfrom foo.bar import baz\n" in code
    assert b"baz.qux()" in code


@pytest.mark.parametrize(
    "section, kind",
    [("nonexist", "win-ia32"), ("console", "nonexist"), ("nonexist", "nonexist")],
)
def test_script_generate_launcher_error(section, kind):
    script = Script("foo", "foo.bar", "baz.qux", section=section)
    with pytest.raises(InvalidScript):
        script.generate("#!C:\\path to my\\python.exe\n", kind=kind)


@pytest.mark.parametrize("section", ["console", "gui"])
@pytest.mark.parametrize("kind", ["win-ia32", "win-amd64", "win-arm"])
def test_script_generate_reproducible_launcher_metadata(section, kind):
    epoch = 1_700_000_000  # 2023-11-14 22:13:20 UTC
    script = Script("foo", "foo.bar", "baz.qux", section=section)
    name, data = script.generate(
        r"C:\path to my\python.exe", kind=kind, source_date_epoch=epoch
    )

    assert name == "foo.exe"
    infos = _embedded_zip_info(data)

    # Fixed member order -- exactly one, deterministically named member.
    assert [info.filename for info in infos] == ["__main__.py"]

    info = infos[0]
    # Fixed member time (UTC), instead of the current wall-clock time.
    assert info.date_time == (2023, 11, 14, 22, 13, 20)
    # Fixed compression, permissions and host/platform metadata.
    assert info.compress_type == zipfile.ZIP_STORED
    assert info.external_attr == (stat.S_IFREG | 0o644) << 16
    assert info.create_system == 3
    assert info.flag_bits == 0
    assert info.internal_attr == 0
    assert info.create_version == 20
    assert info.extract_version == 20


def test_script_generate_reproducible_launcher_is_deterministic():
    epoch = 1_700_000_000
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    _, data_one = script.generate(
        r"C:\Python\python.exe", kind="win-amd64", source_date_epoch=epoch
    )
    _, data_two = script.generate(
        r"C:\Python\python.exe", kind="win-amd64", source_date_epoch=epoch
    )
    assert data_one == data_two


def test_script_generate_reproducible_launcher_default_epoch():
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    _, data = script.generate(
        r"C:\Python\python.exe", kind="win-amd64", source_date_epoch=315532800
    )
    info = _embedded_zip_info(data)[0]
    assert info.date_time == (1980, 1, 1, 0, 0, 0)


@pytest.mark.parametrize("epoch", [0, -1, 4_354_819_200])
def test_script_generate_reproducible_launcher_rejects_bad_epoch(epoch):
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    with pytest.raises(InvalidSourceDateEpoch):
        script.generate(r"C:\Python\python.exe", kind="win-amd64", source_date_epoch=epoch)


def test_script_generate_reproducible_posix_has_no_zip():
    # POSIX launchers don't contain a ZIP and keep the existing simple /
    # complex-path shebang rules when an epoch is supplied.
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    name, data = script.generate(
        "/path to my/python", kind="posix", source_date_epoch=1_700_000_000
    )
    assert name == "foo"
    assert data.startswith(b"#!/bin/sh\n")
    assert b"PK\x03\x04" not in data
