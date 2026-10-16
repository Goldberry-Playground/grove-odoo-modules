#!/usr/bin/env python3
"""Register the Printify webhooks for one environment (spec §7). No dashboard clicking.

Usage (Josh's terminal, secrets from 1Password):
  PRINTIFY_API_TOKEN=... PRINTIFY_SHOP_ID=... PRINTIFY_WEBHOOK_SECRET=... \
    python3 scripts/printify_register_webhooks.py https://odoo.gatheringatthegrove.com/grove/api/v1/printify/webhook
"""

import importlib.util
import os
import sys

_CLIENT = os.path.join(os.path.dirname(__file__), "..", "grove_headless", "models", "printify_client.py")
_spec = importlib.util.spec_from_file_location("printify_client", _CLIENT)
printify_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(printify_client)

TOPICS = ("order:sent-to-production", "order:shipment:created", "order:shipment:delivered", "order:updated")


def register(token, shop_id, url, secret, create=None):
    create = create or printify_client.create_webhook
    return [create(token, shop_id, topic, url, secret) for topic in TOPICS]


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    token, shop_id, secret = (
        os.environ.get(k, "") for k in ("PRINTIFY_API_TOKEN", "PRINTIFY_SHOP_ID", "PRINTIFY_WEBHOOK_SECRET")
    )
    if not (token and shop_id and secret):
        print("Set PRINTIFY_API_TOKEN, PRINTIFY_SHOP_ID and PRINTIFY_WEBHOOK_SECRET.")
        return 2
    for hook in register(token, shop_id, argv[1], secret):
        print("registered", hook.get("topic"), hook.get("id"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
