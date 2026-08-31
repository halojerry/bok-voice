#!/usr/bin/env python3
"""Generate a simple branded 512x512 PNG icon for the desktop shell.

CI derives .icns / .ico from this via `npx @tauri-apps/cli icon icon.png`.
Run locally with:
    python3 desktop/scripts/gen_icon.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


W = H = 512


def px(x: int, y: int) -> tuple[int, int, int, int]:
    # Deep teal background + bright teal "B" rounded blob in the centre.
    if 90 <= x <= 420 and 90 <= y <= 420:
        dx, dy = (x - 255), (y - 255)
        if dx * dx + dy * dy > 175 * 175:
            return (16, 60, 74, 255)  # teal-900 ring
        # Horizontal "B" bars
        if (120 <= y <= 190) or (235 <= y <= 305) or (350 <= y <= 420):
            if 150 <= x <= 360:
                return (72, 220, 232, 255)  # bright teal
    return (8, 34, 45, 255)  # teal-950


def main() -> Path:
    rows = []
    for y in range(H):
        row = bytearray()
        for x in range(W):
            r, g, b, a = px(x, y)
            row += bytes((r, g, b, a))
        rows.append(bytes(row))
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    out = Path(__file__).resolve().parents[1] / "src-tauri" / "icons" / "icon.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return out


if __name__ == "__main__":
    main()
