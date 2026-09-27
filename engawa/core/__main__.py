"""python -m engawa.core でコアサーバーを起動する。"""

import logging

import uvicorn

from engawa.config import load_settings
from engawa.core.app import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.core_host, port=settings.core_port)


if __name__ == "__main__":
    main()
