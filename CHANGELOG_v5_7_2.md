# v5.7.2 — VK send hotfix

- Regular VK replies no longer send `intent=customer_support`.
- VK error 943 (`Cannot use this intent`) now triggers one safe retry without the optional intent.
- Newsletter semantics remain unchanged: `non_promo_newsletter` is still sent explicitly and is never silently downgraded to a regular message.
- Added regression tests for normal replies and error 943 fallback.
