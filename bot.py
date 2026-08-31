#!/usr/bin/env python3
"""Entry point — loads full app module."""
from hstream_app import app, ensure_dependencies, logger

if __name__ == "__main__":
    logger.info("Checking dependencies…")
    ensure_dependencies()
    logger.info("Starting HStream-TG with wzgram (MTProto)…")
    app.run()
