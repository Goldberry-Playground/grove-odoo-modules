#!/usr/bin/env python3
"""Bootstrap a Ghost instance and extract its Content API key.

Idempotent: skips setup if the admin already exists, skips integration
creation if one with the same name already exists.

Usage:
    GHOST_URL=http://localhost:2368 \
    GHOST_ADMIN_EMAIL=admin@goldberrygrove.farm \
    GHOST_ADMIN_PASSWORD='YourPassword!2026' \
    GHOST_ADMIN_NAME='Goldberry Grove' \
    GHOST_BLOG_TITLE='Goldberry Grove Farm' \
    GHOST_INTEGRATION_NAME='Goldberry Next.js Frontend' \
    python3 setup_ghost_integration.py

On success prints a single line: GHOST_CONTENT_KEY=<key>
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

GHOST_URL = os.getenv("GHOST_URL", "http://localhost:2368").rstrip("/")
ADMIN_EMAIL = os.getenv("GHOST_ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("GHOST_ADMIN_PASSWORD")
ADMIN_NAME = os.getenv("GHOST_ADMIN_NAME", "Site Admin")
BLOG_TITLE = os.getenv("GHOST_BLOG_TITLE", "Ghost Blog")
INTEGRATION_NAME = os.getenv("GHOST_INTEGRATION_NAME", "Headless Frontend")


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def request(method: str, path: str, body: dict | None, opener) -> tuple[int, dict]:
    url = f"{GHOST_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Origin": GHOST_URL}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(req) as resp:
            payload = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(payload) if payload.strip() else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body_text)
        except json.JSONDecodeError:
            return e.code, {"raw": body_text}


def main() -> None:
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        fail("GHOST_ADMIN_EMAIL and GHOST_ADMIN_PASSWORD are required")

    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    # 1. Check if Ghost has been set up already.
    code, payload = request("GET", "/ghost/api/admin/authentication/setup/", None, opener)
    if code != 200:
        fail(f"Could not read Ghost setup status (HTTP {code}): {payload}")
    setup_done = bool(payload.get("setup", [{}])[0].get("status"))

    # 2. If not, run setup.
    if not setup_done:
        print(f"Setting up Ghost admin {ADMIN_EMAIL} ...", file=sys.stderr)
        code, payload = request(
            "POST",
            "/ghost/api/admin/authentication/setup/",
            {
                "setup": [
                    {
                        "name": ADMIN_NAME,
                        "email": ADMIN_EMAIL,
                        "password": ADMIN_PASSWORD,
                        "blogTitle": BLOG_TITLE,
                    }
                ]
            },
            opener,
        )
        if code != 201:
            fail(f"Ghost setup failed (HTTP {code}): {payload}")
    else:
        print(f"Ghost already set up; logging in as {ADMIN_EMAIL} ...", file=sys.stderr)

    # 3. Authenticate (creates session cookie).
    code, payload = request(
        "POST",
        "/ghost/api/admin/session/",
        {"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        opener,
    )
    if code not in (200, 201):
        fail(f"Ghost login failed (HTTP {code}): {payload}")

    # 4. Find or create the Custom Integration.
    code, payload = request("GET", "/ghost/api/admin/integrations/", None, opener)
    if code != 200:
        fail(f"Could not list integrations (HTTP {code}): {payload}")
    existing = next(
        (i for i in payload.get("integrations", []) if i.get("name") == INTEGRATION_NAME),
        None,
    )

    if existing:
        print(f"Reusing existing integration: {INTEGRATION_NAME}", file=sys.stderr)
        integration = existing
    else:
        print(f"Creating integration: {INTEGRATION_NAME}", file=sys.stderr)
        code, payload = request(
            "POST",
            "/ghost/api/admin/integrations/",
            {"integrations": [{"name": INTEGRATION_NAME}]},
            opener,
        )
        if code != 201:
            fail(f"Integration create failed (HTTP {code}): {payload}")
        integration = payload["integrations"][0]

    # 5. Pull the content key out and emit it on stdout.
    content_key = next(
        (k for k in integration.get("api_keys", []) if k.get("type") == "content"),
        None,
    )
    if not content_key:
        fail("Integration has no content API key (Ghost may have changed its schema)")
    print(f"GHOST_CONTENT_KEY={content_key['secret']}")


if __name__ == "__main__":
    main()
