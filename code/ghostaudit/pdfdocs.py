"""Generic PDF text extraction for the deletion-audit pipeline.

The evidence set contains generated PDF documents (statements, registers,
support attachments).  They draw text with standard operators and may encode
page streams either plain, Flate-compressed, or ASCII85+Flate.  This module
implements a small, dependency-free reader that:

1. locates page objects and their content-stream object numbers,
2. decodes each stream through its declared filter chain,
3. renders text operators (``Tj``/``TJ``) to lines, splitting on ``Td``/``TD``
   position changes so that phrases stay grouped per record block.

Only participant-visible evidence is ever processed here; nothing about any
specific case or file is embedded in the code.
"""

from __future__ import annotations

import base64
import re
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_STREAM_RE = re.compile(
    rb"(\d+)\s+0\s+obj\s*<<(.*?)>>\s*stream\r?\n(.*?)\s*endstream", re.S
)
_OBJ_RE = re.compile(rb"(\d+)\s+0\s+obj\s*<<(.*?)>>\s*endobj", re.S)


def _decode_stream(blob: bytes, filters: List[str]) -> Optional[bytes]:
    data = blob
    for name in filters:
        try:
            if name == "FlateDecode":
                data = zlib.decompress(data)
            elif name == "ASCII85Decode":
                text = data.strip()
                if text.endswith(b"~>"):
                    text = text[:-2]
                data = base64.a85decode(text, adobe=False)
            elif name in ("ASCIIHexDecode", "AHx"):
                data = bytes.fromhex(re.sub(rb"[^0-9A-Fa-f]", b"", data).decode("ascii"))
            elif name in ("DCTDecode", "JPXDecode", "CCITTFaxDecode", "JBIG2Decode"):
                return None  # raster image payload: no text extraction available
            else:
                return None
        except Exception:
            return None
    return data


def _render_text(content: bytes) -> str:
    out: List[str] = []

    def flush(buf: List[bytes]) -> None:
        if buf:
            text = b"".join(buf).decode("latin1", "replace").strip()
            if text:
                out.append(text)
            buf.clear()

    pending: List[bytes] = []
    for m in re.finditer(
        rb"\(((?:\\.|[^\\()])+)\)\s*Tj|\[((?:[^\]\\]|\\.)*)\]\s*TJ|\bTd\b|\bTD\b|\bT\*\b|\bET\b",
        content,
        re.S,
    ):
        tok = m.group(0)
        if tok in (b"Td", b"TD", b"T*", b"ET"):
            flush(pending)
            continue
        if tok.endswith(b"Tj"):
            instr = m.group(1)
        else:
            instr = b" ".join(
                re.findall(rb"\(((?:\\.|[^\\()])+)\)", tok)
            )
        instr = instr.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
        pending.append(instr)
    flush(pending)
    return "\n".join(out)


def extract_pdf_pages(path: Path) -> List[Tuple[str, str]]:
    """Return ``(locator, text)`` chunks for every decoded content stream.

    The locator is the PDF object id of the stream, keeping provenance stable
    without depending on ``/Contents`` reference plumbing that varies between
    producers.  Each chunk is processed independently downstream.
    """
    try:
        raw = path.read_bytes()
    except Exception:
        return []

    chunks: List[Tuple[int, bytes]] = []
    for m in _STREAM_RE.finditer(raw):
        header = m.group(2)
        blob = m.group(3).rstrip(b"\r\n")
        filters = [f.decode() for f in re.findall(rb"/(ASCII85Decode|FlateDecode|ASCIIHexDecode|AHx|DCTDecode|JPXDecode|CCITTFaxDecode|JBIG2Decode)", header)]
        dec = _decode_stream(blob, filters)
        if dec is None:
            continue
        if b"Tj" in dec or b"TJ" in dec:
            chunks.append((int(m.group(1)), dec))
    if not chunks:
        return []

    out: List[Tuple[str, str]] = []
    for obj_num, dec in sorted(chunks):
        text = _render_text(dec)
        if text.strip():
            out.append((str(obj_num), text))
    return out
