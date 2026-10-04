"""Verify demo Teacher/Admin permissions after role policy changes."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from modules.auth.login import DEMO_USERS


def main() -> None:
    checks = []
    for username, meta in DEMO_USERS.items():
        if not meta.get("password"):
            continue
        payload = json.dumps({"username": username, "password": meta["password"]}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/auth/demo-login",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode())
            user = body.get("user") or {}
            perms = set(user.get("permissions") or [])
            checks.append(
                {
                    "username": username,
                    "role": user.get("role"),
                    "has_reviews": "reviews.manage" in perms,
                    "perm_count": len(perms),
                    "ok": True,
                }
            )
        except urllib.error.HTTPError as exc:
            checks.append(
                {
                    "username": username,
                    "ok": False,
                    "error": f"HTTP {exc.code}: {exc.read().decode(errors='ignore')[:200]}",
                }
            )
        except Exception as exc:  # noqa: BLE001
            checks.append({"username": username, "ok": False, "error": str(exc)})

    print(json.dumps(checks, ensure_ascii=False, indent=2))
    teacher = next((item for item in checks if item.get("role") == "Teacher" and item.get("ok")), None)
    admin = next((item for item in checks if item.get("role") == "Admin" and item.get("ok")), None)
    assert teacher and teacher["has_reviews"], teacher
    assert admin and not admin["has_reviews"], admin
    print("permission_checks_passed")


if __name__ == "__main__":
    main()
