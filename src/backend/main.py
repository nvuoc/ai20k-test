"""Import main(text, session_id=...) or run this file as a text conversation CLI."""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.text import TextBot, main, main_async

__all__ = ["main", "main_async", "TextBot"]


def cli():
    parser = argparse.ArgumentParser(
        description="ParrotGo: lời khách dạng text → lời bot dạng text"
    )
    parser.add_argument("text", nargs="?", help="Một câu của khách; bỏ trống để hội thoại liên tục")
    parser.add_argument(
        "--session", default="cli", help="Mã hội thoại; dùng lại để tiếp tục sau restart"
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.text is not None:
        print(main(args.text, session_id=args.session))
        return

    async def converse():
        async with TextBot() as bot:
            print("ParrotGo · Nhập lời khách. Gõ /thoat để kết thúc.")
            while True:
                try:
                    text = await asyncio.to_thread(input, "Khách: ")
                except EOFError:
                    break
                if text.strip() == "/thoat":
                    break
                if text.strip():
                    print("Bot: " + await bot.ask(text, session_id=args.session))

    asyncio.run(converse())


if __name__ == "__main__":
    cli()
