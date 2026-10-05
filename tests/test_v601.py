from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.booking import AppsScriptBookingBackend
from app.version import VERSION


class BookingTransportHotfixTests(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return SimpleNamespace(
            booking_api_url="https://script.google.com/macros/s/TEST/exec",
            booking_api_secret="secret",
            booking_timeout_seconds=5.0,
        )

    async def _backend_with_handler(self, handler):
        backend = AppsScriptBookingBackend(self.settings())
        await backend.client.aclose()
        backend.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=5.0,
            follow_redirects=False,
        )
        return backend

    async def test_apps_script_redirect_is_followed_as_get_and_404_is_retried_from_exec(self):
        state = {"redirect_no": 0, "post_actions": [], "redirect_methods": []}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "script.google.com":
                body = json.loads(request.content.decode("utf-8"))
                state["post_actions"].append(body["action"])
                state["redirect_no"] += 1
                token = state["redirect_no"]
                return httpx.Response(
                    302,
                    headers={"Location": f"https://script.googleusercontent.com/macros/echo?token={token}"},
                    request=request,
                )
            if request.url.host == "script.googleusercontent.com":
                state["redirect_methods"].append(request.method)
                token = request.url.params.get("token")
                if token == "1":
                    return httpx.Response(200, json={"ok": True, "result": {"healthy": True, "version": "5.9.1"}}, request=request)
                if token == "2":
                    return httpx.Response(404, text="expired one-time url", request=request)
                if token == "3":
                    return httpx.Response(200, json={"ok": True, "result": {"found": True, "enabled": True, "service_name": "Беседка"}}, request=request)
            return httpx.Response(500, request=request)

        backend = await self._backend_with_handler(handler)
        try:
            health = await backend.call("health", {})
            self.assertTrue(health["healthy"])
            service = await backend.call("get_service", {"service_key": "gazebo"})
            self.assertTrue(service["enabled"])
            self.assertEqual(state["post_actions"], ["health", "get_service", "get_service"])
            self.assertEqual(state["redirect_methods"], ["GET", "GET", "GET"])
        finally:
            await backend.close()

    async def test_retry_reuses_same_request_id_for_mutation(self):
        state = {"posts": [], "redirect_no": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "script.google.com":
                body = json.loads(request.content.decode("utf-8"))
                if body["action"] == "create_hold":
                    state["posts"].append(body)
                state["redirect_no"] += 1
                return httpx.Response(
                    302,
                    headers={"Location": f"https://script.googleusercontent.com/macros/echo?token={state['redirect_no']}"},
                    request=request,
                )
            if request.url.host == "script.googleusercontent.com":
                token = request.url.params.get("token")
                if token == "1":
                    return httpx.Response(404, request=request)
                return httpx.Response(200, json={"ok": True, "result": {"available": True, "booking_id": "ZV-TEST"}}, request=request)
            return httpx.Response(500, request=request)

        backend = await self._backend_with_handler(handler)
        try:
            result = await backend.call("create_hold", {"service_key": "gazebo"})
            self.assertEqual(result["booking_id"], "ZV-TEST")
            self.assertEqual(len(state["posts"]), 2)
            first = state["posts"][0]
            second = state["posts"][1]
            self.assertEqual(first["request_id"], second["request_id"])
            self.assertEqual(first["payload"]["request_id"], second["payload"]["request_id"])
        finally:
            await backend.close()

    def test_release_version(self):
        self.assertEqual(VERSION, "6.0.3")


if __name__ == "__main__":
    unittest.main()
