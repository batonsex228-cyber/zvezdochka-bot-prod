from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    required = [
        ROOT / "app" / "main.py",
        ROOT / "app" / "core.py",
        ROOT / "app" / "vk_adapter.py",
        ROOT / "app" / "vk_keyboards.py",
        ROOT / "data" / "seed_faq.json",
        ROOT / "data" / "knowledge_base.json",
        ROOT / "requirements.txt",
    ]
    missing = [str(p.relative_to(ROOT)) for p in required if not p.exists()]
    if missing:
        print("[PREFLIGHT] Missing files:", ", ".join(missing))
        return 1
    for p in [ROOT / "data" / "seed_faq.json", ROOT / "data" / "knowledge_base.json"]:
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[PREFLIGHT] Invalid {p.name}: {exc}")
            return 1
    print("[PREFLIGHT] Project files and bundled knowledge base: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
