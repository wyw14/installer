```{caution}
This API is not finalised, and may change in a patch version.
```

# `installer.components`

`installer.components` provides selective installation of wheel contents.
Callers split the wheel's regular files into named *components*, using
wheel-relative POSIX path patterns, and choose which components to install.

Patterns support `*` and `?` within a single path segment (neither matches a
`/`), and a standalone `**` segment that matches zero or more path segments.
Absolute paths, `.`/`..` segments, backslashes and `**` embedded inside a
segment are rejected. Pattern matching happens before `.data` relocation and
never reads the host file system.

The `METADATA` and `WHEEL` files in `.dist-info`, and the generated
`RECORD` file, are always installed and cannot be excluded. Files that no
rule matches follow the `default` policy. Console and GUI entry points are
assigned to components individually by name; unassigned entry points follow
the same default policy. A kept entry point must be backed (statically) by
its module's `.py` file or its package's `__init__.py` among the kept files,
unless the module is listed in `exempt_modules` (e.g. extension modules).

```{eval-rst}
.. automodule:: installer.components
    :members:
```
