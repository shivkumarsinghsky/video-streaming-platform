"""Run the prototype API: python -m video [--port 8000] [--storage ./data]"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from video.api import create_app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--storage", type=Path, default=Path("data"))
    args = p.parse_args()
    secret = os.environ.get("CDN_SIGNING_SECRET", "dev-only-secret").encode()
    uvicorn.run(create_app(args.storage, secret), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
