"""Shared parsing budgets for workspace and attached spreadsheets."""
from __future__ import annotations

import io
from pathlib import Path
from typing import BinaryIO
import zipfile

from fastapi import HTTPException


def validate_xlsx_archive(
    source: bytes | Path | BinaryIO,
    *,
    max_entries: int = 4096,
    max_uncompressed_bytes: int = 64 * 1024 * 1024,
    max_member_bytes: int = 32 * 1024 * 1024,
    max_compression_ratio: int = 200,
) -> None:
    """Reject unsafe ZIP metadata before openpyxl loads shared strings/XML.

    Path and file callers inspect the central directory without reading the
    whole compressed workbook into memory. The caller retains ownership of a
    supplied binary file. No archive member is extracted to disk.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source) as archive:
            infos = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise HTTPException(
            422, "failed to parse spreadsheet (file may be corrupt or unsupported)",
        ) from None
    if len(infos) > max_entries:
        raise HTTPException(422, "spreadsheet archive exceeds safe entry budget")
    total_uncompressed = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise HTTPException(422, "encrypted spreadsheets are not supported")
        if info.file_size > max_member_bytes:
            raise HTTPException(422, "spreadsheet archive member exceeds safe size budget")
        total_uncompressed += info.file_size
        if total_uncompressed > max_uncompressed_bytes:
            raise HTTPException(422, "spreadsheet archive exceeds safe unpacked size budget")
        if info.file_size > max(1, info.compress_size) * max_compression_ratio:
            raise HTTPException(422, "spreadsheet archive exceeds safe compression ratio")
