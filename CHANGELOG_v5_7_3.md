# v5.7.3 — Site URL hotfix

- Production website base moved from `https://zvezdaglazov.ru/new/` to `https://zvezdaglazov.ru/`.
- Updated crawler defaults, action links, bundled KB source URLs, examples and test fixtures.
- Added backward-compatible migration: an existing server `.env` with `SITE_URL=https://zvezdaglazov.ru/new/` is automatically normalized to the new root URL at runtime.
- No VK/API secrets need to be changed for this hotfix.
