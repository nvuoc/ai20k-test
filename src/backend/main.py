"""Import main(text, session_id=...) or run this file as a text conversation CLI."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from app.contracts.booking import Customer
from app.text import TextBot, main, main_async

__all__ = ["main", "main_async", "TextBot"]


def cli():
    parser = argparse.ArgumentParser(
        description="ParrotGo: lời khách dạng text → lời bot dạng text"
    )
    parser.add_argument("text", nargs="?", help="Một câu của khách; bỏ trống để hội thoại liên tục")
    parser.add_argument("--phone", help="Số điện thoại khách hàng")
    parser.add_argument("--name", help="Tên khách hàng")
    parser.add_argument(
        "--session", default=str(uuid.uuid4()), help="Khóa hội thoại; dùng lại để tiếp tục sau restart"
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    phone = args.phone or input("Số điện thoại: ").strip()
    name = args.name or input("Tên khách hàng: ").strip()
    try:
        customer = Customer(customer_phone=phone, customer_name=name)
    except ValueError:
        parser.error("Tên không được trống và số điện thoại phải hợp lệ.")
    phone, name = customer.customer_phone, customer.customer_name
    if args.text is not None:
        print(main(args.text, session_id=args.session, customer_phone=phone, customer_name=name))
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
                    print("Bot: " + await bot.ask(text, session_id=args.session,
                                                customer_phone=phone, customer_name=name))

    asyncio.run(converse())


if __name__ == "__main__":
    cli()
