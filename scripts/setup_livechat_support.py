#!/usr/bin/env python3
r"""GOL-2021 — configure the Grove support livechat channel + capture chatbot.

Source of truth: vault doc "Software/Grove Support Chat.md", ratified by Josh
2026-09-02. This is *step 2* of the rollout (channel & chatbot config); step 1
(installing `im_livechat,website_livechat,crm` on QA) lands via odoocker
`AUTO_UPGRADE_MODULES` + `scripts/module-upgrade.sh`. Run this only once the
modules are installed (i.e. `/im_livechat/loader` no longer 404s).

Ratified scope: all-Odoo-19-Community, Nursery-only, ONE livechat channel. The
bot answers nothing itself — it greets 24/7, captures the visitor's question +
email, then hands to a human operator (Josh/Wes). The lead/partner glue is
Issue 2's `grove_support` module; this issue only needs the bot capturing.

Idempotent: re-running reconciles the channel, its rule, the chatbot script and
its steps in place (matched by name/title), and provisions Wes's operator user
without duplicating anything. Safe to re-run after a QA reseed.

Usage:
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com \
    ODOO_DB=odoo \
    ODOO_USER=josh@goldberrygrove.farm \
    ODOO_PASSWORD=... \
    WES_EMAIL=wes@example.com \        # optional; skipped (warned) if unset
    WES_NAME='Wes ...' \
    python3 setup_livechat_support.py

    DRY_RUN=1 python3 setup_livechat_support.py   # plan only, no writes

The bot-capture flow (greeting -> question -> email -> thanks -> operator) is
the GOL-2021 acceptance target. Wes's account is optional here so the
acceptance-critical channel/bot config never blocks on an email we don't have.
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client
from typing import Any

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069").rstrip("/")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
DRY_RUN = os.getenv("DRY_RUN", "").strip() not in ("", "0", "false", "False")

# The single Nursery-scoped channel + its chatbot. Names are the idempotency
# keys — do not rename casually or a re-run creates a second copy.
CHANNEL_NAME = os.getenv("GROVE_LIVECHAT_CHANNEL", "At The Grove Nursery Support")
CHATBOT_TITLE = os.getenv("GROVE_CHATBOT_TITLE", "Nursery Support Greeter")
NURSERY_COMPANY_NAME = os.getenv("NURSERY_COMPANY_NAME", "At The Grove Nursery")

# Operators answer live from Odoo Discuss/mobile when available, by email
# otherwise. Josh is always an operator; Wes is added when we have his login.
JOSH_LOGIN = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
WES_EMAIL = os.getenv("WES_EMAIL", "").strip()
WES_NAME = os.getenv("WES_NAME", "Wes").strip()


# ---------------------------------------------------------------------------
# Pure, unit-testable core (no network). Kept at module top so the tests can
# import it without an Odoo connection.
# ---------------------------------------------------------------------------

# Chatbot step types that create a crm.lead. The `grove_support` module (Issue
# 2) is the SOLE lead owner, so the Grove script must hand off with a plain
# `forward_operator` step and must never use these — otherwise Odoo's own
# crm_livechat double-creates leads. See grove_support/docs/grove-support-chat.md
# ("Deployment note — avoid double leads").
LEAD_CREATING_STEP_TYPES = frozenset({"create_lead", "create_lead_and_forward"})


def build_chatbot_steps() -> list[dict[str, Any]]:
    """The ordered capture script: greet 24/7, capture question + email, hand off.

    The bot answers nothing itself. `free_input_multi` captures the free-text
    question; `question_email` captures + validates the email (im_livechat
    normalises it); `forward_operator` hands to a human (Josh/Wes) without
    creating a lead here.
    """
    return [
        {
            "sequence": 1,
            "step_type": "text",
            "message": (
                "Hi! Welcome to At The Grove Nursery. Ask us anything about "
                "our plants, orders, or growing advice and we'll get back to you."
            ),
        },
        {
            "sequence": 2,
            "step_type": "free_input_multi",
            "message": "What can we help you with today?",
        },
        {
            "sequence": 3,
            "step_type": "question_email",
            "message": "Great — what's the best email to reach you at?",
        },
        {
            "sequence": 4,
            "step_type": "text",
            "message": (
                "Thanks! We've got your question. An operator will follow up "
                "here in the chat or by email."
            ),
        },
        {
            "sequence": 5,
            "step_type": "forward_operator",
            "message": "Connecting you with someone from the Grove...",
        },
    ]


def assert_capture_only(steps: list[dict[str, Any]]) -> None:
    """Guard the ratified shape: an email-collecting, non-lead-creating script."""
    types = [s["step_type"] for s in steps]
    if "question_email" not in types:
        raise ValueError("chatbot script must collect an email (question_email step)")
    if not any(t in ("free_input_multi", "free_input_single") for t in types):
        raise ValueError("chatbot script must capture a free-text question")
    bad = LEAD_CREATING_STEP_TYPES.intersection(types)
    if bad:
        raise ValueError(
            f"chatbot script must not create leads (found {sorted(bad)}); "
            "grove_support owns lead creation — hand off with forward_operator"
        )


def wes_user_write_commands(user_group_id: int, portal_group_id: int, has_portal: bool) -> dict[str, Any]:
    """Odoo-19 group write for making Wes an *internal* user.

    Two traps that bit us 2026-08-31 (both encoded here):
      1. The field is ``group_ids``, NOT ``groups_id``.
      2. If Wes already has a portal account, drop ``base.group_portal`` in the
         SAME write that adds ``base.group_user`` — the disjoint-groups
         (portal/internal exclusivity) constraint rejects it otherwise.
    """
    commands = [(4, user_group_id)]
    if has_portal:
        commands.insert(0, (3, portal_group_id))
    return {"group_ids": commands}


def channel_rule_vals(channel_id: int, chatbot_script_id: int) -> dict[str, Any]:
    """A rule that keeps the widget ALWAYS visible with the bot driving it.

    ``action='display_button'`` shows the launcher on every page (``regex_url``
    matches all), and attaching ``chatbot_script_id`` means the bot greets 24/7
    regardless of operator online status — never hidden-when-offline.
    """
    return {
        "channel_id": channel_id,
        "regex_url": "/",
        "action": "display_button",
        "chatbot_script_id": chatbot_script_id,
        "sequence": 10,
    }


# ---------------------------------------------------------------------------
# Imperative XML-RPC upserts (network). Everything below is skipped under
# DRY_RUN=1 for the actual writes; the plan is still printed.
# ---------------------------------------------------------------------------


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def authenticate() -> tuple[Any, int]:
    if not ODOO_PASSWORD:
        fail("ODOO_PASSWORD env var is required")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        fail(f"Authentication failed for user {ODOO_USER} on db {ODOO_DB}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"Authenticated as uid={uid} on db={ODOO_DB} ({ODOO_URL})")
    return models, uid


def _call(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def _plan(action: str, detail: str) -> None:
    tag = "PLAN" if DRY_RUN else "APPLY"
    print(f"  [{tag}] {action}: {detail}")


def ref_id(models, uid, xmlid: str) -> int:
    """Resolve a module.name external id to its res_id (like env.ref)."""
    module, name = xmlid.split(".", 1)
    rows = _call(
        models, uid, "ir.model.data", "search_read",
        [[["module", "=", module], ["name", "=", name]]],
        {"fields": ["res_id"], "limit": 1},
    )
    if not rows:
        fail(f"external id not found: {xmlid}")
    return rows[0]["res_id"]


def preflight_modules(models, uid) -> None:
    """Refuse to run until the step-1 modules are actually installed."""
    installed = _call(
        models, uid, "ir.module.module", "search_read",
        [[["name", "in", ["im_livechat", "website_livechat", "crm"]], ["state", "=", "installed"]]],
        {"fields": ["name"]},
    )
    names = {m["name"] for m in installed}
    missing = {"im_livechat", "website_livechat", "crm"} - names
    if missing:
        fail(
            f"required modules not installed: {sorted(missing)} — run step 1 "
            "(odoocker AUTO_UPGRADE_MODULES + scripts/module-upgrade.sh) first"
        )
    print(f"Preflight OK — installed: {sorted(names)}")


def upsert_chatbot(models, uid) -> int:
    steps = build_chatbot_steps()
    assert_capture_only(steps)
    existing = _call(
        models, uid, "chatbot.script", "search_read",
        [[["title", "=", CHATBOT_TITLE]]], {"fields": ["id"], "limit": 1},
    )
    step_cmds = [(5, 0, 0)] + [(0, 0, s) for s in steps]  # replace all steps
    if existing:
        script_id = existing[0]["id"]
        _plan("chatbot.script", f"update #{script_id} '{CHATBOT_TITLE}' ({len(steps)} steps)")
        if not DRY_RUN:
            _call(models, uid, "chatbot.script", "write", [[script_id], {"step_ids": step_cmds}])
        return script_id
    _plan("chatbot.script", f"create '{CHATBOT_TITLE}' ({len(steps)} steps)")
    if DRY_RUN:
        return 0
    return _call(models, uid, "chatbot.script", "create", [{"title": CHATBOT_TITLE, "step_ids": step_cmds}])


def resolve_operators(models, uid) -> list[int]:
    logins = [JOSH_LOGIN]
    if WES_EMAIL:
        logins.append(WES_EMAIL)
    rows = _call(
        models, uid, "res.users", "search_read",
        [[["login", "in", logins]]], {"fields": ["id", "login"]},
    )
    found = {r["login"]: r["id"] for r in rows}
    for login in logins:
        if login not in found:
            print(f"  WARNING: operator login not found, skipping: {login}", file=sys.stderr)
    return [found[login] for login in logins if login in found]


def upsert_channel(models, uid, chatbot_script_id: int, operator_ids: list[int]) -> int:
    vals: dict[str, Any] = {
        "name": CHANNEL_NAME,
        "user_ids": [(6, 0, operator_ids)],
        "default_message": (
            "Hi! Welcome to At The Grove Nursery. How can we help you today?"
        ),
    }
    # Company scoping is version-dependent on im_livechat.channel. Set it only
    # if the model actually carries the field (verified live via fields_get),
    # so a missing field never hard-fails the acceptance-critical config.
    fields = _call(models, uid, "im_livechat.channel", "fields_get", [], {"attributes": ["type"]})
    company_field = next((f for f in ("company_id", "company_ids") if f in fields), None)
    if company_field:
        companies = _call(
            models, uid, "res.company", "search_read",
            [[["name", "=", NURSERY_COMPANY_NAME]]], {"fields": ["id"], "limit": 1},
        )
        if companies:
            cid = companies[0]["id"]
            vals[company_field] = cid if company_field == "company_id" else [(6, 0, [cid])]
            _plan("im_livechat.channel", f"scope to company '{NURSERY_COMPANY_NAME}' via {company_field}")
        else:
            print(f"  WARNING: company '{NURSERY_COMPANY_NAME}' not found — channel left unscoped", file=sys.stderr)

    existing = _call(
        models, uid, "im_livechat.channel", "search_read",
        [[["name", "=", CHANNEL_NAME]]], {"fields": ["id"], "limit": 1},
    )
    if existing:
        channel_id = existing[0]["id"]
        _plan("im_livechat.channel", f"update #{channel_id} '{CHANNEL_NAME}' (ops={operator_ids})")
        if not DRY_RUN:
            _call(models, uid, "im_livechat.channel", "write", [[channel_id], vals])
    else:
        _plan("im_livechat.channel", f"create '{CHANNEL_NAME}' (ops={operator_ids})")
        channel_id = 0 if DRY_RUN else _call(models, uid, "im_livechat.channel", "create", [vals])

    # One always-visible rule with the bot attached. Reconcile in place.
    if channel_id:
        rule_vals = channel_rule_vals(channel_id, chatbot_script_id)
        rules = _call(
            models, uid, "im_livechat.channel.rule", "search_read",
            [[["channel_id", "=", channel_id]]], {"fields": ["id"]},
        )
        if rules:
            rule_id = rules[0]["id"]
            _plan("im_livechat.channel.rule", f"update #{rule_id} action=display_button + chatbot")
            if not DRY_RUN:
                _call(models, uid, "im_livechat.channel.rule", "write",
                      [[rule_id], {k: v for k, v in rule_vals.items() if k != "channel_id"}])
        else:
            _plan("im_livechat.channel.rule", "create action=display_button + chatbot (regex_url=/)")
            if not DRY_RUN:
                _call(models, uid, "im_livechat.channel.rule", "create", [rule_vals])
    return channel_id


def provision_wes(models, uid) -> None:
    if not WES_EMAIL:
        print(
            "  Wes operator account SKIPPED — set WES_EMAIL (+ WES_NAME) to provision. "
            "Josh: I need Wes's email/login to create his internal user.",
            file=sys.stderr,
        )
        return
    user_group_id = ref_id(models, uid, "base.group_user")
    portal_group_id = ref_id(models, uid, "base.group_portal")
    existing = _call(
        models, uid, "res.users", "search_read",
        [[["login", "=", WES_EMAIL]]], {"fields": ["id", "group_ids", "active"]},
        # NB Odoo 19: the field is group_ids, NOT groups_id.
    )
    if existing:
        row = existing[0]
        has_portal = portal_group_id in (row.get("group_ids") or [])
        already_internal = user_group_id in (row.get("group_ids") or [])
        if already_internal:
            print(f"  Wes user #{row['id']} already internal — no change")
            return
        cmds = wes_user_write_commands(user_group_id, portal_group_id, has_portal)
        _plan("res.users", f"promote #{row['id']} to internal (has_portal={has_portal}) via group_ids")
        if not DRY_RUN:
            _call(models, uid, "res.users", "write", [[row["id"]], cmds])
    else:
        vals = {
            "name": WES_NAME,
            "login": WES_EMAIL,
            "email": WES_EMAIL,
            **wes_user_write_commands(user_group_id, portal_group_id, has_portal=False),
        }
        _plan("res.users", f"create internal user '{WES_NAME}' <{WES_EMAIL}>")
        if not DRY_RUN:
            _call(models, uid, "res.users", "create", [vals])


def main() -> None:
    # Fail fast on the pure guard even before we touch the network.
    assert_capture_only(build_chatbot_steps())
    models, uid = authenticate()
    preflight_modules(models, uid)
    print("Configuring Grove support livechat" + (" (DRY RUN — no writes)" if DRY_RUN else "") + ":")
    chatbot_script_id = upsert_chatbot(models, uid)
    operator_ids = resolve_operators(models, uid)
    channel_id = upsert_channel(models, uid, chatbot_script_id, operator_ids)
    provision_wes(models, uid)
    print(
        f"Done. channel={channel_id or '(dry-run)'} chatbot={chatbot_script_id or '(dry-run)'} "
        f"operators={operator_ids}"
    )
    if not DRY_RUN:
        print("Verify: load a nursery page, confirm the widget launcher renders and the bot "
              "greets -> collects question -> collects email.")


if __name__ == "__main__":
    main()
