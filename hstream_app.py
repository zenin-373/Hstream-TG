#!/usr/bin/env python3
"""Load full module from base64 parts hstream_app.b64.*"""
from __future__ import annotations
import base64
from pathlib import Path

_here = Path(__file__).resolve().parent
_parts = sorted(_here.glob("hstream_app.b64.*"), key=lambda p: p.name)
_code = base64.b64decode("".join(p.read_text().strip() for p in _parts)).decode("utf-8")
exec(compile(_code, str(_here / "hstream_app.py"), "exec"), globals())
