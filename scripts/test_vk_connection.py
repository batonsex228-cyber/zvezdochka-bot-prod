from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


async def api(client: httpx.AsyncClient, method: str, token: str, version: str, **params):
    r = await client.post("https://api.vk.com/method/" + method, data={"access_token": token, "v": version, **params})
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"[{err.get('error_code')}] {err.get('error_msg')}")
    return data.get("response")


async def main() -> int:
    load_dotenv(ROOT / ".env")
    token = os.getenv("VK_GROUP_TOKEN", "").strip()
    group_raw = os.getenv("VK_GROUP_ID", "").strip()
    version = os.getenv("VK_API_VERSION", "5.199").strip() or "5.199"
    manager_raw = os.getenv("VK_MANAGER_USER_ID", "").strip()
    content_token = os.getenv("VK_CONTENT_TOKEN", "").strip()
    source_domain = os.getenv("VK_SOURCE_DOMAIN", "zvezdochkaooorazvitie").strip()
    if not token or not group_raw:
        print("VK_GROUP_TOKEN or VK_GROUP_ID is missing.")
        return 2
    try:
        group_id = int(group_raw)
    except ValueError:
        print("VK_GROUP_ID must be numeric without a minus sign.")
        return 2

    manager_status = None
    source_ok = False
    content_status: str | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        try:
            lp = await api(client, "groups.getLongPollServer", token, version, group_id=group_id)
            settings = await api(client, "groups.getLongPollSettings", token, version, group_id=group_id)
        except Exception as exc:
            print(f"VK API: FAILED -> {type(exc).__name__}: {exc}")
            return 1
        try:
            source = await api(client, "groups.getById", token, version, group_id=source_domain, fields="description,status,site")
            groups = (source.get("groups") or []) if isinstance(source, dict) else (source or [])
            source_ok = bool(groups)
        except Exception:
            source_ok = False

        if manager_raw:
            try:
                manager_id = int(manager_raw)
                allowed = await api(client, "messages.isMessagesFromGroupAllowed", token, version, group_id=group_id, user_id=manager_id)
                manager_status = bool(allowed.get("is_allowed")) if isinstance(allowed, dict) else bool(allowed)
            except ValueError:
                manager_status = "INVALID_ID"
            except Exception as exc:
                manager_status = f"CHECK_FAILED: {type(exc).__name__}: {exc}"

        if content_token:
            try:
                # Optional content token is only validated against the public source wall.
                resolved = await api(client, "utils.resolveScreenName", content_token, version, screen_name=source_domain)
                source_id = int((resolved or {}).get("object_id") or 0) if isinstance(resolved, dict) else 0
                if source_id <= 0:
                    content_status = "INVALID_SOURCE"
                else:
                    wall = await api(client, "wall.get", content_token, version, owner_id=-source_id, count=1, filter="owner")
                    content_status = "OK" if isinstance(wall, dict) else "INVALID_RESPONSE"
            except Exception as exc:
                content_status = f"FAILED: {type(exc).__name__}: {exc}"

    print(f"VK API: OK -> group_id={group_id}")
    print(f"Long Poll server: {'OK' if isinstance(lp, dict) and lp.get('server') else 'INVALID'}")
    events = settings.get("events", {}) if isinstance(settings, dict) else {}
    print(f"message_new: {'ON' if events.get('message_new') else 'OFF - enable it'}")
    print(f"message_event: {'ON' if events.get('message_event') else 'OFF - enable it for callback buttons'}")
    print(f"Official VK source profile: {'OK' if source_ok else 'WARNING - could not read'} -> {source_domain}")
    if manager_raw:
        if manager_status is True:
            print(f"VK manager inbox: OK -> user_id={manager_raw}, community messages allowed")
        elif manager_status is False:
            print(f"VK manager inbox: BLOCKED -> user_id={manager_raw}; send any message to the community first")
        else:
            print(f"VK manager inbox: {manager_status}")
    else:
        print("VK manager inbox: NOT CONFIGURED")
    if content_token:
        print(f"Recent VK posts token: {content_status}")
    else:
        print("Recent VK posts: DISABLED (optional; VK_CONTENT_TOKEN not configured)")
    ok = bool(events.get("message_new") and events.get("message_event"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
