"""``python -m chordarr`` / ``chordarr`` entry point."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="chordarr", description="Music collection manager for the *arr stack")
    parser.add_argument("--data", help="data directory (default: CHORDARR_DATA_DIR, /var/lib/chordarr or ~/.config/chordarr)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--reload", action="store_true", help="development auto-reload")
    args = parser.parse_args()
    if args.data:
        os.environ["CHORDARR_DATA_DIR"] = args.data
    if args.host:
        os.environ["CHORDARR_HOST"] = args.host
    if args.port:
        os.environ["CHORDARR_PORT"] = str(args.port)

    import uvicorn

    from chordarr.config import settings

    uvicorn.run("chordarr.app:app", host=settings.host, port=settings.port, reload=args.reload, log_level=settings.log_level)


if __name__ == "__main__":
    main()
