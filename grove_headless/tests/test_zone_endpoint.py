"""Pure tests for the /zone response builder."""

import importlib.util
import pathlib

_path = pathlib.Path(__file__).parent.parent / "controllers" / "product_domain.py"
_spec = importlib.util.spec_from_file_location("product_domain", _path)
product_domain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(product_domain)


def test_known_zone():
    body, status = product_domain.zone_response("25301-1234", 6)
    assert status == 200
    assert body == {"zip": "25301", "zone": 6}


def test_unknown_zone_404():
    body, status = product_domain.zone_response("00000", None)
    assert status == 404
    assert body == {"error": "unknown zip"}
