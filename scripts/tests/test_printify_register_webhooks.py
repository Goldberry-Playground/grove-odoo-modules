import importlib.util
import os
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), "..", "printify_register_webhooks.py")
_spec = importlib.util.spec_from_file_location("printify_register_webhooks", _PATH)
reg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reg)


def test_registers_every_topic():
    create = mock.Mock(side_effect=lambda token, shop, topic, url, secret: {"topic": topic})
    out = reg.register("tok", "42", "https://odoo.example/grove/api/v1/printify/webhook", "s", create=create)
    assert [o["topic"] for o in out] == list(reg.TOPICS)
    assert set(reg.TOPICS) == {
        "order:sent-to-production",
        "order:shipment:created",
        "order:shipment:delivered",
        "order:updated",
    }
