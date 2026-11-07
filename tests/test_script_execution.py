"""End-to-end execution tests for POSIX interpreter arguments.

These tests build a real wheel, install it with ``interpreter_args``
configured, and execute the generated entry-point script and the rewritten
wheel-provided script through a POSIX shell:

* a "fake interpreter" records its argv, proving that spaces, single quotes,
  non-ASCII paths/arguments, empty arguments and literal shell metacharacters
  all cross the shell boundary with exact byte boundaries;
* a real CPython (reached through a shim in a path containing spaces and
  non-ASCII characters) proves that ``-I``/``-X`` and the caller's arguments
  arrive in the correct order, flags take effect, and the exit code is
  propagated by ``exec``.

The tests are skipped automatically when no POSIX shell is available.
"""

import base64
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import installer
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile

ENTRY_POINTS = b"""\
[console_scripts]
fakecli = fakepkg:main
"""

WHEEL_FILE = b"""\
Wheel-Version: 1.0
Generator: execution-tests
Root-Is-Purelib: true
Tag: py3-none-any
"""

METADATA = b"""\
Metadata-Version: 2.1
Name: fake
Version: 1.0
"""

PACKAGE_INIT = b"""\
import json
import os
import sys

# The generated entry points must never import the module at installation
# time; the test sets this variable while running the installer.
if os.environ.get("FAKE_NO_IMPORT_AT_INSTALL"):
    raise RuntimeError("fakepkg was imported during installation")


def main():
    payload = {
        "argv": sys.argv,
        "isolated": sys.flags.isolated,
        "utf8_mode": sys.flags.utf8_mode,
    }
    sys.stdout.write("JSON:" + json.dumps(payload, ensure_ascii=False) + "\\n")
    return int(os.environ.get("FAKE_EXIT_CODE", "0"))
"""

# A script shipped *inside* the wheel with the ``#!python`` placeholder.
WHEEL_SCRIPT = b"""\
#!python
import json
import os
import sys

payload = {
    "argv": sys.argv,
    "isolated": sys.flags.isolated,
    "utf8_mode": sys.flags.utf8_mode,
}
sys.stdout.write("JSON:" + json.dumps(payload, ensure_ascii=False) + "\\n")
sys.exit(int(os.environ.get("FAKE_EXIT_CODE", "0")))
"""

FAKE_INTERPRETER = b"""\
#!/bin/sh
# Record argv exactly as received: one value per line, count on the first line.
: > "$FAKE_RECORD"
printf '%s\\n' "$#" >> "$FAKE_RECORD"
for a in "$@"; do
    printf '%s\\n' "$a" >> "$FAKE_RECORD"
done
"""

RUNNER_RECORD = b"""\
#!/bin/sh
cd "$1" || exit 99
interp="$2"
script="$3"
record="$4"
chmod +x "$interp" "$script"
shift 4
FAKE_RECORD="$record" "$script" "$@"
"""

RUNNER_REAL = b"""\
#!/bin/sh
cd "$1" || exit 99
interp="$2"
script="$3"
code="$4"
chmod +x "$interp" "$script"
shift 4
FAKE_EXIT_CODE="$code" "$script" "$@"
printf '\\nEXIT:%s\\n' "$?"
"""


def _posix_shell() -> str | None:
    if os.name == "posix":
        return shutil.which("sh")
    candidates = [
        r"C:\msys64\usr\bin\sh.exe",
        r"C:\Program Files\Git\bin\sh.exe",
        r"C:\Program Files\Git\usr\bin\sh.exe",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return shutil.which("sh")


@pytest.fixture
def posix_sh() -> str:
    shell = _posix_shell()
    if shell is None:
        pytest.skip("no POSIX shell available")
    return shell


def _to_shell_path(path: str | Path) -> str:
    path = str(path)
    if os.name == "nt" and re.match(r"^[A-Za-z]:[\\/]", path):
        return "/" + path[0].lower() + "/" + path[3:].replace("\\", "/")
    return path


def _build_wheel(wheel_path: Path) -> None:
    members: list[tuple[str, bytes, bool]] = [
        ("fakepkg/__init__.py", PACKAGE_INIT, False),
        ("fake-1.0.data/scripts/wheelscript", WHEEL_SCRIPT, True),
        ("fake-1.0.dist-info/entry_points.txt", ENTRY_POINTS, False),
        ("fake-1.0.dist-info/WHEEL", WHEEL_FILE, False),
        ("fake-1.0.dist-info/METADATA", METADATA, False),
        ("fake-1.0.dist-info/top_level.txt", b"fakepkg\n", False),
    ]

    record_rows = []
    for name, data, _executable in members:
        digest = hashlib.sha256(data).digest()
        hash_value = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        record_rows.append(f"{name},sha256={hash_value},{len(data)}")
    record_rows.append("fake-1.0.dist-info/RECORD,,")

    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name, data, executable in members:
            info = zipfile.ZipInfo(name)
            if executable:
                info.external_attr = (stat.S_IFREG | 0o755) << 16
            archive.writestr(info, data)
        archive.writestr("fake-1.0.dist-info/RECORD", ("\n".join(record_rows) + "\n"))


@pytest.fixture
def installed(tmp_path: Path):
    def _install(
        interpreter: str,
        interpreter_args: list[str],
        instance: str = "default",
    ) -> dict[str, Path]:
        instance_dir = tmp_path / instance
        wheel_path = instance_dir / "fake-1.0-py3-none-any.whl"
        instance_dir.mkdir(parents=True)
        _build_wheel(wheel_path)

        scripts_dir = instance_dir / "prefix" / "bin"
        scripts_dir.mkdir(parents=True)
        # Point purelib/platlib at the scripts directory so that the entry
        # point module is importable as a sibling of the generated script
        # (its directory is sys.path[0], except under CPython's -I -- the
        # isolated-mode run uses the self-contained wheel-provided script).
        scheme_dict = {
            "purelib": str(scripts_dir),
            "platlib": str(scripts_dir),
            "headers": str(instance_dir / "prefix" / "include"),
            "scripts": str(scripts_dir),
            "data": str(instance_dir / "prefix" / "data"),
        }
        for path in scheme_dict.values():
            Path(path).mkdir(parents=True, exist_ok=True)

        destination = SchemeDictionaryDestination(
            scheme_dict,
            interpreter=interpreter,
            script_kind="posix",
            interpreter_args=interpreter_args,
        )

        # Must not import (or byte-compile) the entry point's module.
        os.environ["FAKE_NO_IMPORT_AT_INSTALL"] = "1"
        try:
            with WheelFile.open(wheel_path) as source:
                installer.install(source, destination, additional_metadata={})
        finally:
            os.environ.pop("FAKE_NO_IMPORT_AT_INSTALL", None)

        return {
            "scripts_dir": scripts_dir,
            "fakecli": scripts_dir / "fakecli",
            "wheelscript": scripts_dir / "wheelscript",
            "record": scripts_dir / "fake-1.0.dist-info" / "RECORD",
        }

    return _install


def _record_for(record_path: Path, filename: str) -> tuple[str, int]:
    # The scripts scheme may be recorded with a "./" prefix, a relative
    # prefix, or (on Windows) an absolute path prefix.
    with record_path.open(encoding="utf-8", newline="") as stream:
        for row in csv.reader(stream):
            normalized = row[0].replace("\\", "/").removeprefix("./")
            if normalized == filename or normalized.endswith("/" + filename):
                return row[1], int(row[2])
    raise AssertionError(f"{filename} not found in RECORD: {record_path}")


def _assert_record_matches(paths: dict[str, Path], filename: str) -> None:
    file_path = paths[filename]
    data = file_path.read_bytes()
    hash_value, size = _record_for(paths["record"], filename)
    assert size == len(data)
    digest = hashlib.sha256(data).digest()
    expected = "sha256=" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert hash_value == expected


def _run(
    posix_sh: str, cwd: Path, runner: bytes, *args: str
) -> subprocess.CompletedProcess[bytes]:
    runner_path = cwd / "_runner.sh"
    runner_path.write_bytes(runner)
    return subprocess.run(
        [posix_sh, str(runner_path), ".", *args],
        cwd=str(cwd),
        capture_output=True,
        check=False,
    )


def test_record_hashes_match_wrapped_bytes(installed):
    interpreter = "../interps/Python Env/bin/python"
    paths = installed(interpreter, ["-I", "-X", "utf8"])

    # The on-disk bytes carry the wrapper; RECORD must hash those bytes.
    _assert_record_matches(paths, "fakecli")
    _assert_record_matches(paths, "wheelscript")

    data = paths["fakecli"].read_bytes()
    assert data.startswith(
        b"#!/bin/sh\n'''exec' '../interps/Python Env/bin/python' -I -X utf8 "
        b'"$0" "$@"\n'
    )
    assert b"from fakepkg import main\n" in data
    # The wheel script keeps its body byte-for-byte after the wrapper.
    wheel_data = paths["wheelscript"].read_bytes()
    assert wheel_data.endswith(WHEEL_SCRIPT[len(b"#!python\n") :])


def test_fake_interpreter_argv_boundaries(installed, posix_sh):
    interpreter = "../interps/Pÿthön Énv/bin/fake interp"
    args = [
        "-I",
        "-X",
        "utf8",
        "",
        "with space",
        "it's",
        '"double quotes"',
        "$(echo PWNED)",
        "`echo PWNED`",
        "$HOME",
        "a;b",
        "pipe|me",
        "glob*?",
        "éñ→日本",
    ]
    paths = installed(interpreter, args)
    scripts_dir = paths["scripts_dir"]

    fake_interpreter = (scripts_dir / interpreter).resolve()
    fake_interpreter.parent.mkdir(parents=True, exist_ok=True)
    fake_interpreter.write_bytes(FAKE_INTERPRETER)

    user_args = ["user arg", "ü2"]

    for script_name in ("fakecli", "wheelscript"):
        record_file = f"argv_{script_name}.txt"
        result = _run(
            posix_sh,
            scripts_dir,
            RUNNER_RECORD,
            interpreter,
            f"./{script_name}",
            record_file,
            *user_args,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

        lines = (scripts_dir / record_file).read_bytes().split(b"\n")
        # Trailing newline produces one final empty element.
        assert lines[-1] == b""
        recorded = lines[:-1]
        assert int(recorded[0]) == len(args) + 1 + len(user_args)
        expected = [
            *(arg.encode("utf-8") for arg in args),
            f"./{script_name}".encode(),
            *(arg.encode("utf-8") for arg in user_args),
        ]
        assert recorded[1:] == expected


def _write_python_shim(scripts_dir: Path, interpreter: str) -> None:
    # A POSIX shell shim, in a directory containing a space and non-ASCII
    # characters, that replaces itself with the real interpreter. The wrapper
    # therefore has to quote the shim path correctly for the exec to work.
    real_python = _to_shell_path(sys.executable)
    shim = (scripts_dir / interpreter).resolve()
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_bytes(f'#!/bin/sh\nexec "{real_python}" "$@"\n'.encode())


@pytest.mark.parametrize(
    ("instance", "script_name", "args", "isolated"),
    [
        # The entry-point script proves the module:attr body is still loaded
        # normally (``-I`` would keep sibling packages off sys.path, like for
        # any real script -- the package would live in site-packages).
        ("entry", "fakecli", ["-X", "utf8"], 0),
        # The wheel-provided script is self-contained, so -I can be verified
        # end to end together with -X and the caller's arguments.
        ("isolated", "wheelscript", ["-I", "-X", "utf8"], 1),
    ],
)
def test_real_python_interpreter_args_order_flags_and_exit(
    installed, posix_sh, instance, script_name, args, isolated
):
    interpreter = "../interps/Réal Pÿthon/bin/python"
    paths = installed(interpreter, args, instance=instance)
    scripts_dir = paths["scripts_dir"]
    _write_python_shim(scripts_dir, interpreter)

    user_args = ["hello", "spa ce", "éxtra"]
    result = _run(
        posix_sh,
        scripts_dir,
        RUNNER_REAL,
        interpreter,
        f"./{script_name}",
        "42",
        *user_args,
    )
    output = result.stdout.decode("utf-8", "replace")
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    json_lines = [
        line[len("JSON:") :] for line in output.splitlines() if line.startswith("JSON:")
    ]
    assert len(json_lines) == 1, output
    payload = json.loads(json_lines[0])
    assert payload["isolated"] == isolated
    assert payload["utf8_mode"] == 1
    assert payload["argv"] == [f"./{script_name}", *user_args]
    # The wrapper uses ``exec`` without ``sh -c``, so Python's own exit status
    # (42) is the process status observed by the runner.
    assert "\nEXIT:42\n" in output
