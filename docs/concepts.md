# Concepts

This library has two main abstractions:

- {any}`WheelSource`: Serves as source of information about a wheel file.
- {any}`WheelDestination`: Handles all file writing and post-installation
  processing.

## WheelSource

These objects represent a wheel file, abstracting away how the actual file is
stored or accessed.

This allows the core install logic to be used with in-memory wheel files, or
unzipped-on-disk wheel, or with {any}`zipfile.ZipFile` objects from an on-disk
wheel, or something else entirely.

This protocol/abstraction is designed to be implementable without a direct
dependency on this library. This allows for other libraries in the Python
packaging ecosystem to provide implementations of the protocol, allowing for
more code reuse opportunities.

One of the benefits of this fully described interface is the possibility to
decouple the implementation of additional validation on wheels (such as
validating the RECORD entries in a wheel match the actual contents of the wheel,
or enforcing signing requirements) based on what the specific use case demands.

## WheelDestination

These objects are responsible for handling the writing-to-filesystem
interactions, determining RECORD file entries and post-install actions (like
generating .pyc files). While this is a lot of responsibility, this was
explicitly provided to make it possible for custom `WheelDestination`
implementations to be more powerful and flexible.

Most of these tasks can either be delegated to utilities provided in this
library (eg: script generation), or to the Python standard library (eg:
generating `.pyc` files).

## Reproducible installations

{any}`SchemeDictionaryDestination` accepts a ``reproducible`` flag that
produces explicit, reproducible installation artifacts. When the same wheel
is installed with the same scheme paths, interpreter and optimization levels
-- into different ``destdir`` staging roots, on the same Python minor version
-- every installed file, generated entry script and the ``RECORD`` file are
byte-for-byte identical. Filesystem timestamps of the written files are not
part of the artifacts and are intentionally left untouched.

In this mode:

- Bytecode uses {pep}`552` **checked-hash** invalidation, so ``.pyc`` files
  embed a hash of the source contents rather than the source's modification
  time or size. The embedded ``co_filename`` is the file's final runtime path
  (computed from ``scheme_dict``); the staging directory and any temporary
  directories never leak into the bytecode.
- Only the requested optimization levels (``0``, ``1`` and ``2``) are
  compiled. Duplicate levels are de-duplicated and invalid levels are
  rejected. A compilation failure reports the logical source path and fails
  the installation, rather than being silently ignored.
- Windows entry launchers embed ZIP archives whose member date/time, order,
  permissions and compression are all fixed, independent of the current
  wall-clock time. POSIX launchers keep their existing simple shebang and
  complex-path ``/bin/sh`` wrapper behaviour.
- Generated ``.pyc`` files are included in ``RECORD`` with the hash and size
  of the bytes actually written.

The fixed timestamp is controlled by ``source_date_epoch`` (a POSIX
timestamp, interpreted in UTC), mirroring the ``SOURCE_DATE_EPOCH``
environment variable convention. It must fit in a ZIP archive member's
date/time fields (1980-01-01 00:00:00 UTC through 2107-12-31 23:59:58 UTC);
other values are rejected. When not provided,
{any}`installer.utils.DEFAULT_SOURCE_DATE_EPOCH` (1980-01-01 00:00:00 UTC) is
used.

Bytecode is inherently specific to the Python minor version that produced it,
so reproducibility of ``.pyc`` files is only guaranteed within the same minor
version.
