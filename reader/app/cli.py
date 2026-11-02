from __future__ import annotations

import argparse
import time

from .config import Settings
from .signing import admin_login_token, article_token


def main() -> None:
    parser = argparse.ArgumentParser(description="SMTH reader token utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login-url")
    login_parser.add_argument("--ttl", type=int, default=900)

    article_parser = subparsers.add_parser("article-url")
    article_parser.add_argument("--board", required=True)
    article_parser.add_argument("--topic", required=True)
    article_parser.add_argument("--title", default="")
    article_parser.add_argument("--ttl", type=int, default=7776000)

    args = parser.parse_args()
    settings = Settings.from_env()

    if args.command == "login-url":
        token = admin_login_token(settings.secret, int(time.time()) + args.ttl)
        print(f"{settings.public_base}/login/{token}")
        return

    token = article_token(
        settings.secret,
        args.board,
        args.topic,
        args.title,
        int(time.time()) + args.ttl,
    )
    print(f"{settings.public_base}/a/{token}")


if __name__ == "__main__":
    main()
