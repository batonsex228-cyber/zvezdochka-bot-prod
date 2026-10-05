from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.booking import AppsScriptBookingBackend, BookingAPIError


async def run() -> int:
    load_dotenv(ROOT / ".env")
    url = os.getenv("BOOKING_API_URL", "").strip()
    secret = os.getenv("BOOKING_API_SECRET", "").strip()
    try:
        timeout = float(os.getenv("BOOKING_TIMEOUT_SECONDS", "15"))
    except ValueError:
        timeout = 15.0
    if not url or not secret:
        print("BOOKING_API_URL or BOOKING_API_SECRET is missing.")
        return 2
    settings = SimpleNamespace(
        booking_api_url=url, booking_api_secret=secret, booking_timeout_seconds=max(3.0, min(timeout, 60.0))
    )
    backend = AppsScriptBookingBackend(settings)
    try:
        health = await backend.call("health", {})
        if health.get("healthy") is not True:
            print("Booking API health check returned an unexpected response:", health)
            return 3
        print(f"Booking API: OK — v{health.get('version') or '?'}")
        for key, label in (("gazebo", "Беседка"), ("corpus", "Корпус")):
            result = await backend.call("get_service", {"service_key": key})
            if result.get("found") and result.get("enabled"):
                price = result.get("price_amount")
                unit = result.get("price_unit")
                price_text = f" · {int(price):,} ₽ {unit}".replace(",", " ") if price else ""
                print(f"{label}: ENABLED — {result.get('service_name') or key}{price_text}")
            else:
                print(f"{label}: disabled/not configured (safe)")
        return 0
    except BookingAPIError as exc:
        print(f"Booking API: FAILED — {exc}")
        return 1
    finally:
        await backend.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
