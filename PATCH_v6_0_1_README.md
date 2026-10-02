# PATCH v6.0.1 — BOOKING TRANSPORT HOTFIX

Apply this patch over **v6.0.0 FINAL LAST AUDITED**.

The patch fixes intermittent Google Apps Script one-time redirect failures (`script.googleusercontent.com ... 404`) observed in production.

After applying:

1. Run `python scripts/self_test.py`.
2. Update the existing Apps Script project with the bundled `google_apps_script/Code.gs`.
3. Redeploy the same Web App as a new version. Keep the same access settings.
4. Keep the same `BOOKING_API_URL` and `BOOKING_API_SECRET` unless Google issues a new deployment URL.
5. Run `python scripts/test_booking_connection.py` on the server.

No database reset is required. No Google Sheet recreation is required.
