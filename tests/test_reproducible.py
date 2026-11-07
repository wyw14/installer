"""End-to-end tests for the explicit reproducible artifacts mode."""

import base64
import hashlib
import importlib.util
import marshal
import os
import stat
import sys
import textwrap
import uuid
import zipfile
from pathlib import Path

import pytest

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.exceptions import (
    BytecodeCompilationError,
    InvalidBytecodeOptimizationLevel,
    InvalidSourceDateEpoch,
)
from installer.records import RecordEntry, parse_record_file
from installer.sources import WheelFile
from installer.utils import SCHEME_NAMES

EPOCH = 1_700_000_000  # 2023-11-14 22:13:20 UTC
OLD_MTIME = 500_000_000  # 1985 -- different from "now" on any machine
NEWER_MTIME = 1_500_000_000  # 2017

PY_SOURCE_FILES = ("rpbuild/__init__.py", "rpbuild/mod.py")


# --------------------------------------------------------------------------
# Wheel construction
# --------------------------------------------------------------------------
def _build_wheel(directory, *, broken=False):
    """Build a wheel with valid RECORD, entry points and a data-scheme file."""
    files: dict[str, bytes] = {
        "rpbuild/__init__.py": b"value = 1\n",
        # The module-level raise proves installation compiles but never
        # imports/executes the wheel's code.
        "rpbuild/mod.py": textwrap.dedent('''\
            """Module docstring."""
            raise RuntimeError("wheel module executed during installation")


            def add(a, b):
                return a + b
        ''').encode(),
        "rpbuild-1.0.data/data/rpbuild/data.txt": b"data payload\n",
        "rpbuild-1.0.data/scripts/rpbuild-tool": b"#!python\nprint('tool')\n",
        "rpbuild-1.0.dist-info/top_level.txt": b"rpbuild\n",
        "rpbuild-1.0.dist-info/entry_points.txt": textwrap.dedent("""\
            [console_scripts]
            rpbuild = rpbuild.mod:add

            [gui_scripts]
            rpbuild-gui = rpbuild.mod:add
        """).encode(),
        "rpbuild-1.0.dist-info/WHEEL": textwrap.dedent("""\
            Wheel-Version: 1.0
            Generator: reproducible-tests
            Root-Is-Purelib: true
            Tag: py3-none-any
        """).encode(),
        "rpbuild-1.0.dist-info/METADATA": textwrap.dedent("""\
            Metadata-Version: 2.1
            Name: rpbuild
            Version: 1.0
        """).encode(),
    }
    if broken:
        files["rpbuild/broken.py"] = b"def broken(:\n"

    path = directory / "rpbuild-1.0-py3-none-any.whl"
    rows = []
    with zipfile.ZipFile(path, "w") as archive:
        for name in sorted(files):
            data = files[name]
            archive.writestr(name, data)
            digest = base64.urlsafe_b64encode(
                hashlib.sha256(data).digest()
            ).decode().rstrip("=")
            rows.append(f"{name},sha256={digest},{len(data)}")
        # executable bit on the raw script in the scripts scheme
        script_member = "rpbuild-1.0.data/scripts/rpbuild-tool"
        archive.getinfo(script_member).external_attr = (
            stat.S_IFREG | 0o755
        ) << 16
        rows.append("rpbuild-1.0.dist-info/RECORD,,")
        archive.writestr(
            "rpbuild-1.0.dist-info/RECORD", ("\n".join(rows) + "\n").encode()
        )
    return path


@pytest.fixture()
def wheel(tmp_path):
    return _build_wheel(tmp_path)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def make_destination(
    logical,
    stage,
    *,
    kind="posix",
    interpreter="/opt/python/bin/python",
    levels=(0, 1, 2),
    reproducible=True,
    epoch=EPOCH,
    overwrite_existing=False,
):
    scheme_dict = {scheme: str(logical / scheme) for scheme in SCHEME_NAMES}
    return SchemeDictionaryDestination(
        scheme_dict=scheme_dict,
        interpreter=interpreter,
        script_kind=kind,
        bytecode_optimization_levels=levels,
        destdir=str(stage),
        reproducible=reproducible,
        source_date_epoch=epoch,
        overwrite_existing=overwrite_existing,
    )


def install_wheel(wheel, destination):
    with WheelFile.open(wheel) as source:
        install(source, destination, {})


def file_tree(root):
    """Map of POSIX-style relative path -> file bytes under ``root``."""
    return {
        str(path.relative_to(root)).replace(os.sep, "/"): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def iter_code_objects(code):
    yield code
    for const in code.co_consts:
        if hasattr(const, "co_consts"):
            yield from iter_code_objects(const)


def read_pyc(path):
    data = path.read_bytes()
    return data, int.from_bytes(data[4:8], "little"), marshal.loads(data[16:])


def pyc_tag():
    return f"cpython-{sys.version_info[0]}{sys.version_info[1]}"


# --------------------------------------------------------------------------
# Byte-for-byte reproducibility across different staging roots
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kind", "interpreter"),
    [
        ("posix", "/opt/python/bin/python"),  # simple shebang
        ("posix", "/opt with spaces/python"),  # /bin/sh wrapper
        ("win-amd64", r"C:\Python\python.exe"),  # launcher + embedded ZIP
    ],
)
def test_installs_to_different_destdirs_are_byte_identical(
    tmp_path, wheel, kind, interpreter
):
    logical = tmp_path / "logical-prefix"
    stage_a = tmp_path / f"stage-{uuid.uuid4().hex}"
    stage_b = tmp_path / f"stage-{uuid.uuid4().hex}"
    assert stage_a != stage_b

    install_wheel(
        wheel,
        make_destination(
            logical, stage_a, kind=kind, interpreter=interpreter
        ),
    )
    install_wheel(
        wheel,
        make_destination(
            logical, stage_b, kind=kind, interpreter=interpreter
        ),
    )

    tree_a = file_tree(stage_a)
    tree_b = file_tree(stage_b)
    assert tree_a.keys() == tree_b.keys()

    differing = sorted(name for name in tree_a if tree_a[name] != tree_b[name])
    assert differing == [], f"files differ between staging roots: {differing}"

    # Bytecode and launchers were really produced (no post-install deletion).
    pyc_files = [name for name in tree_a if name.endswith(".pyc")]
    assert len(pyc_files) == 6  # 2 modules x 3 optimization levels
    script_files = [
        name
        for name in tree_a
        if "/scripts/" in name and "rpbuild" in Path(name).name
    ]
    assert script_files  # generated entry scripts exist


def test_wheel_file_is_not_modified_or_executed(tmp_path, wheel):
    before = wheel.read_bytes()
    logical = tmp_path / "logical-prefix"
    stage = tmp_path / "stage"
    # Would raise (and fail) if rpbuild.mod, which raises on import, got
    # executed -- installation must only compile the sources.
    install_wheel(
        wheel,
        make_destination(logical, stage, levels=(0, 1, 2)),
    )
    assert wheel.read_bytes() == before


def test_pyc_independent_of_source_mtime(tmp_path, wheel):
    logical = tmp_path / "logical-prefix"
    stage = tmp_path / "stage"
    destination = make_destination(logical, stage, levels=(0, 1))
    install_wheel(wheel, destination)

    pyc_before = {
        str(path.relative_to(stage)): path.read_bytes()
        for path in stage.rglob("*.pyc")
    }
    assert pyc_before

    # Change the source files' mtime, then re-run bytecode compilation.
    for source in stage.rglob("*.py"):
        os.utime(source, (OLD_MTIME, OLD_MTIME))
    recompiler = make_destination(
        logical, stage, levels=(0, 1), overwrite_existing=True
    )
    for rel in PY_SOURCE_FILES:
        recompiler._compile_bytecode("purelib", RecordEntry(rel, None, None))

    pyc_after_old = {
        str(path.relative_to(stage)): path.read_bytes()
        for path in stage.rglob("*.pyc")
    }
    assert pyc_after_old == pyc_before

    for source in stage.rglob("*.py"):
        os.utime(source, (NEWER_MTIME, NEWER_MTIME))
    for rel in PY_SOURCE_FILES:
        recompiler._compile_bytecode("purelib", RecordEntry(rel, None, None))
    pyc_after_newer = {
        str(path.relative_to(stage)): path.read_bytes()
        for path in stage.rglob("*.pyc")
    }
    assert pyc_after_newer == pyc_before


# --------------------------------------------------------------------------
# checked-hash pyc header and embedded filenames
# --------------------------------------------------------------------------
def test_checked_hash_pyc_header_and_co_filename(tmp_path, wheel):
    logical = tmp_path / "logical-prefix"
    stage = tmp_path / "stage"
    destination = make_destination(logical, stage, levels=(0, 1, 2))
    install_wheel(wheel, destination)

    logical_purelib = Path(destination.scheme_dict["purelib"])
    pyc_files = sorted(stage.rglob("*.pyc"))
    assert {path.name.split(".", 1)[1] for path in pyc_files} == {
        f"{pyc_tag()}.pyc",
        f"{pyc_tag()}.opt-1.pyc",
        f"{pyc_tag()}.opt-2.pyc",
    }

    for pyc_path in pyc_files:
        data, flags, code = read_pyc(pyc_path)

        # PEP 552 hash-based pyc: bit 0 set; checked-hash also sets bit 1,
        # per CPython's ``_code_to_hash_pyc``.
        assert flags & 0b1, f"{pyc_path.name} is not hash-based: flags={flags:#b}"
        assert flags & 0b10, f"{pyc_path.name} is not checked-hash: flags={flags:#b}"

        # The embedded hash must be the source_hash of the actual .py bytes;
        # there is no mtime/size payload to leak filesystem state.
        source_name = pyc_path.stem.split(".")[0]
        source_path = next(p for p in stage.rglob(f"{source_name}.py"))
        source_bytes = source_path.read_bytes()
        assert data[8:16] == importlib.util.source_hash(source_bytes)
        assert len(data) == 16 + len(marshal.dumps(code))

        # Every code object names the final runtime location only -- never
        # the staging (destdir) root or any temporary directory.
        for nested in iter_code_objects(code):
            assert nested.co_filename.startswith(str(logical_purelib))
            assert str(stage) not in nested.co_filename
        assert str(stage).encode() not in data


# --------------------------------------------------------------------------
# RECORD: real hashes/sizes of every final file, existing cross-scheme paths
# --------------------------------------------------------------------------
def test_record_hashes_match_installed_files(tmp_path, wheel):
    logical = tmp_path / "logical-prefix"
    stage = tmp_path / "stage"
    destination = make_destination(logical, stage, levels=(0, 1))
    install_wheel(wheel, destination)

    record_path = next(stage.rglob("RECORD"))
    rows = list(parse_record_file(record_path.read_text().splitlines()))

    # The .pyc artifacts are recorded, as are generated/cross-scheme files.
    row_paths = {row[0] for row in rows}
    pyc_rows = [row for row in rows if row[0].endswith(".pyc")]
    assert len(pyc_rows) == 4  # 2 modules x levels 0, 1
    assert any("/scripts/rpbuild" in path for path in row_paths)
    assert any("data/rpbuild/data.txt" in path for path in row_paths)

    anchor = Path(logical.anchor)
    for rel, hash_value, size in rows:
        if not hash_value:
            # The RECORD row itself has no hash/size.
            assert rel.endswith("RECORD")
            continue
        # Resolve the row relative to the root scheme (purelib), including
        # the existing cross-scheme relative/absolute prefix semantics.
        joined = str(Path(destination.scheme_dict["purelib"]) / rel)
        resolved_logical = Path(os.path.normpath(joined))
        installed = stage / resolved_logical.relative_to(anchor)
        assert installed.is_file(), f"RECORD references missing file: {rel}"

        data = installed.read_bytes()
        algorithm, value = hash_value.split("=", 1)
        digest = base64.urlsafe_b64encode(
            hashlib.new(algorithm, data).digest()
        ).decode().rstrip("=")
        assert value == digest, f"hash mismatch for {rel}"
        assert int(size) == len(data), f"size mismatch for {rel}"


# --------------------------------------------------------------------------
# Optimization level handling
# --------------------------------------------------------------------------
def test_duplicate_optimization_levels_are_deduplicated(tmp_path, wheel):
    logical = tmp_path / "logical-prefix"
    stage = tmp_path / "stage"
    destination = make_destination(logical, stage, levels=[1, 1, 0, 0, 2, 1])
    assert destination.bytecode_optimization_levels == (1, 0, 2)
    install_wheel(wheel, destination)

    pyc_names = {path.name for path in stage.rglob("*.pyc")}
    assert pyc_names == {
        name
        for source_stem in ("__init__", "mod")
        for name in (
            f"{source_stem}.{pyc_tag()}.pyc",
            f"{source_stem}.{pyc_tag()}.opt-1.pyc",
            f"{source_stem}.{pyc_tag()}.opt-2.pyc",
        )
    }


@pytest.mark.parametrize("level", [3, -1, 42, True, "1", 1.5])
def test_invalid_optimization_level_rejected(tmp_path, level):
    logical = tmp_path / "logical-prefix"
    with pytest.raises(InvalidBytecodeOptimizationLevel):
        make_destination(logical, tmp_path / "stage", levels=[level])


def test_no_optimization_levels_compiles_nothing(tmp_path, wheel):
    logical = tmp_path / "logical-prefix"
    stage = tmp_path / "stage"
    destination = make_destination(logical, stage, levels=())
    install_wheel(wheel, destination)
    assert list(stage.rglob("*.pyc")) == []


# --------------------------------------------------------------------------
# SOURCE_DATE_EPOCH handling
# --------------------------------------------------------------------------
def test_source_date_epoch_defaults_to_documented_fixed_time(tmp_path):
    destination = make_destination(
        tmp_path / "logical", tmp_path / "stage", epoch=None
    )
    from installer.utils import DEFAULT_SOURCE_DATE_EPOCH

    assert destination.source_date_epoch == DEFAULT_SOURCE_DATE_EPOCH


@pytest.mark.parametrize(
    "epoch", [0, -1, 315_532_799, 4_354_819_200, "1", True, 10**20]
)
def test_invalid_source_date_epoch_rejected(tmp_path, epoch):
    with pytest.raises(InvalidSourceDateEpoch):
        make_destination(
            tmp_path / "logical", tmp_path / "stage", epoch=epoch
        )


def test_source_date_epoch_requires_reproducible(tmp_path):
    scheme_dict = {
        scheme: str(tmp_path / "logical" / scheme) for scheme in SCHEME_NAMES
    }
    with pytest.raises(InvalidSourceDateEpoch):
        SchemeDictionaryDestination(
            scheme_dict=scheme_dict,
            interpreter="/opt/python/bin/python",
            script_kind="posix",
            reproducible=False,
            source_date_epoch=EPOCH,
        )


# --------------------------------------------------------------------------
# Compilation failures
# --------------------------------------------------------------------------
def test_compilation_failure_fails_installation_with_logical_path(tmp_path):
    broken_wheel = _build_wheel(tmp_path, broken=True)
    logical = tmp_path / "logical-prefix"
    stage = tmp_path / "stage"
    destination = make_destination(logical, stage, levels=(0,))

    with pytest.raises(BytecodeCompilationError) as exc_info:
        install_wheel(broken_wheel, destination)

    message = str(exc_info.value)
    logical_source = Path(destination.scheme_dict["purelib"]) / "rpbuild/broken.py"
    assert str(logical_source) in message
    assert str(stage) not in message

    # The call fails outright: RECORD is not written on compilation failure.
    assert not destination._path_with_destdir(
        "purelib", "rpbuild-1.0.dist-info/RECORD"
    ).exists()


# --------------------------------------------------------------------------
# Default mode keeps its original behaviour
# --------------------------------------------------------------------------
def test_default_mode_uses_timestamp_pyc_and_wall_clock_launcher(tmp_path, wheel):
    logical = tmp_path / "logical-prefix"
    stage = tmp_path / "stage"
    destination = make_destination(
        logical, stage, kind="win-amd64", interpreter=r"C:\Python\python.exe",
        levels=(0,), reproducible=False, epoch=None,
    )
    install_wheel(wheel, destination)

    pyc_path = next(stage.rglob("__init__*.pyc"))
    data = pyc_path.read_bytes()
    flags = int.from_bytes(data[4:8], "little")
    assert flags & 0b1 == 0  # timestamp-based invalidation in default mode

    # Invalid levels are passed through untouched (no new validation).
    passthrough = make_destination(
        logical,
        tmp_path / "other",
        levels=[3, 3],
        reproducible=False,
        epoch=None,
    )
    assert list(passthrough.bytecode_optimization_levels) == [3, 3]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_reproducible(tmp_path, fancy_wheel, monkeypatch):
    from installer.__main__ import _main as main

    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    destdir = tmp_path / "dest"
    main(
        [
            str(fancy_wheel),
            "-d",
            str(destdir),
            "--reproducible",
            "--compile-bytecode",
            "0",
        ],
        "python -m installer",
    )

    pyc_path = next(destdir.rglob("*.pyc"))
    flags = int.from_bytes(pyc_path.read_bytes()[4:8], "little")
    assert flags & 0b1 and flags & 0b10  # checked hash


def test_cli_reproducible_env_source_date_epoch(tmp_path, fancy_wheel, monkeypatch):
    from installer.__main__ import _main as main

    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    destdir = tmp_path / "dest"
    main(
        [str(fancy_wheel), "-d", str(destdir), "--reproducible"],
        "python -m installer",
    )
    assert destdir.exists()


def test_cli_reproducible_bad_env_source_date_epoch(
    tmp_path, fancy_wheel, monkeypatch
):
    from installer.__main__ import _main as main

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number")
    destdir = tmp_path / "dest"
    with pytest.raises(SystemExit):
        main(
            [str(fancy_wheel), "-d", str(destdir), "--reproducible"],
            "python -m installer",
        )


def test_cli_reproducible_invalid_epoch(tmp_path, fancy_wheel, monkeypatch):
    from installer.__main__ import _main as main

    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    destdir = tmp_path / "dest"
    with pytest.raises(InvalidSourceDateEpoch):
        main(
            [
                str(fancy_wheel),
                "-d",
                str(destdir),
                "--reproducible",
                "--source-date-epoch",
                "0",
            ],
            "python -m installer",
        )


def test_cli_source_date_epoch_requires_reproducible(tmp_path, fancy_wheel, monkeypatch):
    from installer.__main__ import _main as main

    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    destdir = tmp_path / "dest"
    with pytest.raises(InvalidSourceDateEpoch):
        main(
            [
                str(fancy_wheel),
                "-d",
                str(destdir),
                "--source-date-epoch",
                str(EPOCH),
            ],
            "python -m installer",
        )
