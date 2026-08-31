#!/usr/bin/env python3
"""Bootstrap: load full bot from bot.b64.* chunks."""
from __future__ import annotations
import base64
from pathlib import Path

here = Path(__file__).resolve().parent
parts = sorted(here.glob("bot.b64.*"), key=lambda p: int(p.name.rsplit(".", 1)[-1]))
if not parts:
    raise SystemExit("Missing bot.b64.* — restore bot.py")
code = base64.b64decode("".join(p.read_text().strip() for p in parts)).decode("utf-8")
(here / "_bot_expanded.py").write_text(code)
exec(compile(code, str(here / "bot.py"), "exec"), {"__name__": "__main__", "__file__": str(here / "bot.py")})
