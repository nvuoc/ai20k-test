"""Offline demonstration of the text-in/text-out entry point and inquiry flow."""

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.text import TextBot  # noqa: E402


async def run():
    with TemporaryDirectory() as folder:
        settings = Settings(
            profile="test",
            secret="demo",
            database_path=Path(folder) / "app.sqlite",
            checkpoint_path=Path(folder) / "graph.sqlite",
            location_confirmation_enabled=True,
        )
        async with TextBot(settings) as bot:
            for text in [
                "Bạn là ai?",
                "Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, 2 người, xe 4 chỗ, số 0901234567.",
                "Từ Nhà hát Lớn Hà Nội đến Bạch Mai cổng sau bao nhiêu tiền, bao nhiêu km và mất bao lâu?",
                "Dùng tuyến vừa hỏi để đặt",
                "Đồng ý đặt",
                "Bạn là ai?",
                "Hủy đơn",
                "Đồng ý hủy",
                "Thời tiết ở Nhà hát Lớn Hà Nội bây giờ có mưa không?",
            ]:
                print("Khách: " + text)
                print("Bot: " + await bot.ask(text, customer_phone="0901234567", customer_name="An"))
                # All interactions use customer text; no UI actions are needed.
                for _ in range(10):
                    sid = bot.store.find_session("local-text", "default")
                    state = bot.store.snapshot(sid)["state"] if sid else None
                    if not state or state["last_response"]["action"] != "confirm_slots":
                        break
                    print("Khách: đúng")
                    print("Bot: " + await bot.ask("đúng"))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run())
