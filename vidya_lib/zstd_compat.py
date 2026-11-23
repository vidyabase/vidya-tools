"""zstd support across Python versions.

Python 3.14+ ships `compression.zstd` in the standard library. On older
Pythons we fall back to the `zstandard` package (pip install zstandard).

Exposes:
    open(file, mode="rb"|"wb", level=None)  -> binary file object
    ZstdError                                 -> exception raised on bad data
"""

from __future__ import annotations

try:
    from compression import zstd as _std  # Python 3.14+
except ImportError:
    _std = None

if _std is not None:
    ZstdError = _std.ZstdError

    def open(file, mode: str = "rb", level=None):  # noqa: A001
        if "w" in mode:
            return _std.open(file, mode, level=level)
        return _std.open(file, mode)

else:
    try:
        import zstandard as _zs
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "zstd support needs Python 3.14+ or the 'zstandard' package: "
            "pip install -r requirements.txt"
        ) from e

    ZstdError = _zs.ZstdError

    def open(file, mode: str = "rb", level=None):  # noqa: A001
        if "w" in mode:
            cctx = _zs.ZstdCompressor(level=3 if level is None else level)
            return _zs.open(file, mode, cctx=cctx)
        return _zs.open(file, mode)
