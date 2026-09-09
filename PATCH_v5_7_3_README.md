# v5.7.3 SITE URL HOTFIX

Purpose: switch the website knowledge crawler from the retired `/new/` prefix to `https://zvezdaglazov.ru/`.

The patch is backward-compatible with an existing production `.env` that still contains the old `SITE_URL`; the application migrates that value in memory on startup.

After applying the patch, run:

```bash
python scripts/self_test.py
git add .
git commit -m "Hotfix website URL to zvezdaglazov.ru root"
git push
```

GitHub Actions should rebuild the `latest` GHCR image. Then redeploy/pull `ghcr.io/batonsex228-cyber/zvezdochka-bot-prod:latest` on the server.
