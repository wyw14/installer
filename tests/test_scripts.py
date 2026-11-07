import io
import shlex
import zipfile
from pathlib import Path

import pytest

from installer import _scripts
from installer.scripts import InvalidScript, Script


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


def test_script_generate_interpreter_args():
    args = ["-I", "-X", "utf8"]
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    name, data = script.generate("/opt/python/bin/python", "posix", args)

    assert name == "foo"
    assert data.startswith(
        b"#!/bin/sh\n'''exec' /opt/python/bin/python -I -X utf8 \"$0\" \"$@\"\n' '''\n"
    )
    assert b"\nfrom foo.bar import baz\n" in data
    assert b"baz.qux()" in data


def test_script_generate_interpreter_args_force_wrapper_for_simple_path():
    # Even a short, space-free executable gets the wrapper once args exist:
    # relying on kernel shebang argument parsing would be platform-dependent.
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    _, data = script.generate("/python", "posix", ["-I"])

    assert data.startswith(b"#!/bin/sh\n'''exec' /python -I ")
    assert not data.startswith(b"#!/python\n")


def test_script_generate_interpreter_args_order():
    args = ["zzz", "aaa", "-I"]
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    _, data = script.generate("/python", "posix", args)

    exec_line = data.split(b"\n", 3)[1]
    assert exec_line == b"'''exec' /python zzz aaa -I \"$0\" \"$@\""


def test_script_generate_interpreter_args_gui_posix():
    # On POSIX, gui scripts take the same wrapper and the executable is used
    # unchanged (python->pythonw rewriting only happens for Windows launchers).
    script = Script("foo", "foo.bar", "baz.qux", section="gui")
    _, data = script.generate("/opt/bin/python", "posix", ["-I"])

    assert data.startswith(b"#!/bin/sh\n'''exec' /opt/bin/python -I ")


@pytest.mark.parametrize(
    "arg",
    [
        "",
        "with space",
        "it's",
        '"double quotes"',
        "$(echo pwned)",
        "`echo pwned`",
        "$HOME",
        "a;b",
        "a|b",
        "glob*?",
        "éñ→日本",
    ],
)
def test_script_generate_interpreter_args_quoting(arg):
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    _, data = script.generate("/Python Env/bin/python", "posix", [arg])

    exec_line = data.split(b"\n", 3)[1]
    expected = (
        "'''exec' "
        + shlex.quote("/Python Env/bin/python")
        + " "
        + shlex.quote(arg)
        + ' "$0" "$@"'
    ).encode("utf-8")
    assert exec_line == expected
    # The token appears as a single-quoted literal, which the shell cannot
    # expand even when it contains $(), backticks or double quotes.
    quoted = shlex.quote(arg).encode("utf-8")
    assert quoted in exec_line


def test_script_generate_interpreter_args_non_ascii_executable():
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    executable = "/opt/Pÿthön Énv/bin/python"
    _, data = script.generate(executable, "posix", ["-I", ""])

    exec_line = data.split(b"\n", 3)[1]
    assert exec_line == (
        "'''exec' " + shlex.quote(executable) + " -I '' " + '"$0" "$@"'
    ).encode("utf-8")


@pytest.mark.parametrize(
    "kind",
    ["win-ia32", "win-amd64", "win-arm", "win-arm64"],
)
def test_script_generate_interpreter_args_rejected_for_windows_launcher(kind):
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    with pytest.raises(InvalidScript, match="posix"):
        script.generate("C:\\Python\\python.exe", kind, ["-I"])


@pytest.mark.parametrize(
    "executable,args",
    [
        ("/bad\x00python", []),
        ("/bad\npython", []),
        ("/bad\rpython", []),
        ("/python", ["\x00"]),
        ("/python", ["a\nb"]),
        ("/python", ["a\rb"]),
        ("/python", ["ok", "\x00bad"]),
    ],
)
def test_script_generate_rejects_forbidden_characters(executable, args):
    script = Script("foo", "foo.bar", "baz.qux", section="console")
    with pytest.raises(InvalidScript, match="forbidden character"):
        script.generate(executable, "posix", args)
