from __future__ import annotations

import compileall
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TELEGRAM_TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")
VK_TOKEN_HINT_RE = re.compile(r"(?i)(?:vk_group_token|vk_content_token)\s*[=:]\s*[\"']?(?!PASTE|OPTIONAL|$)[A-Za-z0-9._-]{80,}")


def fail(message: str) -> int:
    print(f"FAIL: {message}", flush=True)
    return 1


def main() -> int:
    print("[SELFTEST] Release: v5.7.4 GREETING UX HOTFIX · VK-FIRST", flush=True)

    print("[SELFTEST] Python compile...", flush=True)
    for rel in ["app", "scripts", "tests"]:
        if not compileall.compile_dir(ROOT / rel, quiet=1):
            return fail(f"Python compilation error in {rel}")
        print(f"  OK: {rel}", flush=True)

    print("[SELFTEST] JSON data...", flush=True)
    for rel in ["data/seed_faq.json", "data/knowledge_base.json"]:
        json.loads((ROOT / rel).read_text(encoding="utf-8"))
        print(f"  OK: {rel}", flush=True)
    dev_path = ROOT / ".devcontainer" / "devcontainer.json"
    if dev_path.exists():
        json.loads(dev_path.read_text(encoding="utf-8"))
        print("  OK: .devcontainer/devcontainer.json", flush=True)

    print("[SELFTEST] Secret / runtime isolation scan...", flush=True)
    if (ROOT / ".env").exists():
        return fail(".env must not be included in a distributable build")
    forbidden_files = [
        "app/keyboards.py",
        "scripts/test_telegram_network.py",
        "scripts/setup_env.py",
        "TEST_TELEGRAM_NETWORK.cmd",
    ]
    for rel in forbidden_files:
        if (ROOT / rel).exists():
            return fail(f"obsolete Telegram runtime artifact still present: {rel}")

    runtime_text = ""
    runtime_targets = [ROOT / "app", ROOT / "scripts", ROOT / "requirements.txt", ROOT / "START_CODESPACE.sh", ROOT / "start.sh"]
    for target in runtime_targets:
        paths = [target] if target.is_file() else list(target.rglob("*"))
        for path in paths:
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.resolve() == (ROOT / "scripts" / "self_test.py").resolve():
                continue
            if path.suffix.lower() not in {".py", ".txt", ".sh", ""} and path.name != "requirements.txt":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            runtime_text += "\n" + text
            if TELEGRAM_TOKEN_RE.search(text) or VK_TOKEN_HINT_RE.search(text):
                return fail(f"token-like secret found in {path.relative_to(ROOT)}")
    for forbidden in ["import aiogram", "from aiogram", "TELEGRAM_BOT_TOKEN", "api.telegram.org"]:
        if forbidden in runtime_text:
            return fail(f"production runtime still references {forbidden}")
    if "aiogram" in (ROOT / "requirements.txt").read_text(encoding="utf-8").lower():
        return fail("aiogram is still in requirements.txt")
    print("  OK: no bundled secrets; no Telegram production dependency", flush=True)

    print("[SELFTEST] Unit/integration-offline tests...", flush=True)
    result = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-p", "test_*.py", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    combined = (result.stdout or "") + (result.stderr or "")
    if combined.strip():
        print(combined.rstrip(), flush=True)
    if result.returncode != 0:
        return result.returncode
    m = re.search(r"Ran\s+(\d+)\s+tests?", combined)
    count = int(m.group(1)) if m else 0
    if count < 140:
        return fail(f"unexpectedly low test count: {count}")
    print(f"  OK: {count} tests", flush=True)

    print("[SELFTEST] Offline demo smoke...", flush=True)
    demo = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo_console.py")],
        cwd=ROOT,
        input="📅 Смены и цены\nСколько раз кормят?\nКогда пятая смена?\nEXIT\n",
        text=True,
        capture_output=True,
    )
    if demo.returncode != 0 or "Зимняя смена" not in demo.stdout or "пятиразовое питание" not in demo.stdout or "[WOULD ESCALATE TO HUMAN]" not in demo.stdout:
        print(demo.stdout)
        print(demo.stderr)
        return fail("offline demo smoke test")
    print("  OK: pinned shifts + FAQ + safe escalation", flush=True)

    print("[SELFTEST] Helper scripts...", flush=True)
    for cmd in [
        [sys.executable, str(ROOT / "scripts" / "preflight.py")],
        [sys.executable, str(ROOT / "scripts" / "rebuild_kb.py"), "--help"],
    ]:
        helper = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if helper.returncode != 0:
            print(helper.stdout)
            print(helper.stderr)
            return fail(f"helper script failed: {' '.join(cmd)}")
    print("  OK: preflight + rebuild_kb --help", flush=True)

    print("[SELFTEST] Launchers / Codespaces...", flush=True)
    for name in ["START_CODESPACE.sh", "CHECK_CODESPACE.sh", "start.sh", "scripts/codespace_setup.sh", "scripts/codespace_banner.sh", "APPLY_V571.sh", "APPLY_V572.sh"]:
        path = ROOT / name
        if not path.exists():
            return fail(f"missing launcher {name}")
        shell = subprocess.run(["bash", "-n", str(path)], cwd=ROOT, capture_output=True, text=True)
        if shell.returncode != 0:
            print(shell.stderr)
            return fail(f"shell syntax error in {name}")
        print(f"  OK: {name}", flush=True)

    if dev_path.exists():
        dev = json.loads(dev_path.read_text(encoding="utf-8"))
        if dev.get("postCreateCommand") != "bash scripts/codespace_setup.sh":
            return fail("devcontainer postCreateCommand is not wired to Codespaces setup")
        secrets = dev.get("secrets", {})
        if "VK_GROUP_TOKEN" not in secrets or "VK_GROUP_ID" not in secrets:
            return fail("VK Codespaces secrets are not declared")
        if "TELEGRAM_BOT_TOKEN" in secrets:
            return fail("legacy Telegram secret is still declared")
        print("  OK: VK-first devcontainer secrets", flush=True)

    print("[SELFTEST] Release docs...", flush=True)
    for rel in ["README.md", "CHANGELOG_v5_7.md", "PATCH_v5_7_README.md", "CHANGELOG_v5_7_4.md", "PATCH_v5_7_4_README.md", "KNOWLEDGE_BASE_GUIDE.md", "VK_TEST_SETUP.md", "SERVER_DEPLOY.md"]:
        path = ROOT / rel
        if not path.exists():
            return fail(f"missing release document {rel}")
        text = path.read_text(encoding="utf-8")
        if "5.7" not in text:
            return fail(f"release document does not mention v5.7: {rel}")
    print("  OK: v5.7 release documentation", flush=True)

    print(f"\nALL OFFLINE TESTS PASSED — {count} TESTS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
