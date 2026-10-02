# v6.0.1 — BOOKING TRANSPORT HOTFIX

Hotfix for the production Google Apps Script booking bridge.

## Fixed

- Google Apps Script `ContentService` redirects are now followed explicitly as `GET` requests.
- Expired/one-time `script.googleusercontent.com` redirect URLs are retried from the original `/exec` endpoint.
- Every booking API call gets a stable request id across retries.
- `create_hold` is idempotent in the Apps Script bridge, so a retry cannot create a duplicate hold.
- `submit_booking` and `manager_decision` are idempotent for repeated delivery.
- Added regression tests for the exact live failure: `health` succeeds, the next `get_service` redirect returns 404, retry succeeds.
- Booking bridge version is now 5.9.1; bot release is 6.0.1.

The Google Sheet schema is unchanged. Existing booking rows and server SQLite data are preserved.
