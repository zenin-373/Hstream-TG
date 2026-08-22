#!/usr/bin/env python3
"""Bootstrap: assemble bot from bot.b64.* chunks and exec."""
from __future__ import annotations
import base64
from pathlib import Path

here = Path(__file__).resolve().parent
parts = sorted(here.glob("bot.b64.*"), key=lambda p: int(p.name.rsplit(".", 1)[-1]))
if not parts:
    raise SystemExit("Missing bot.b64.* chunks — re-clone or restore bot.py")
data = "".join(p.read_text().strip() for p in parts)
code = base64.b64decode(data).decode("utf-8")
# Write real bot next to us for debugging, then exec
(here / "_bot_expanded.py").write_text(code)
ns = {"__name__": "__main__", "__file__": str(here / "bot.py")}
exec(compile(code, str(here / "bot.py"), "exec"), ns)
