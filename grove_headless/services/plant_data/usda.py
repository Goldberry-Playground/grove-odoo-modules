"""USDA PLANTS provider (free, no API key). GOL-2383 spec section B.

Synchronous from the Fetch button. 10 s timeout, one retry per call; any
failure becomes a chatter hint, never an exception to the user. Pass ``get=``
(default ``requests.get``) so the Odoo handler and the tests share one path.
"""

from __future__ import annotations

import os

import requests

try:  # package import under the Odoo runtime
    from . import mapping
except ImportError:  # loaded by file path in CI (see tests/test_plant_data.py)
    import sys as _sys

    mapping = _sys.modules.get("grove_plant_mapping")
    if mapping is None:
        import importlib.util as _ilu

        _p = os.path.join(os.path.dirname(__file__), "mapping.py")
        _spec = _ilu.spec_from_file_location("grove_plant_mapping", _p)
        mapping = _ilu.module_from_spec(_spec)
        _sys.modules["grove_plant_mapping"] = mapping  # register before exec (dataclasses)
        _spec.loader.exec_module(mapping)

BASE = "https://plantsservices.sc.egov.usda.gov/api"
TIMEOUT = 10


class USDAProvider:
    def __init__(self, get=requests.get, base: str = BASE):
        self._get = get
        self._base = base.rstrip("/")

    # -- one GET with a single retry; returns parsed JSON or raises last error
    def _fetch(self, path: str, params: dict | None = None):
        last = None
        for _ in range(2):
            try:
                resp = self._get(f"{self._base}/{path}", params=params, timeout=TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001 — retried once, then surfaced as a hint
                last = exc
        raise last

    def lookup(self, botanical_name, cached_id=None) -> "mapping.PlantFacts":
        binomial, skip = mapping.resolve_binomial(botanical_name)
        if skip:
            return mapping.PlantFacts(hints=[f"USDA skipped: {skip}"])

        symbol = cached_id
        try:
            if not symbol:
                results = self._fetch("PlantSearch", {"searchText": binomial})
                match = mapping.usda_pick_exact(results or [], binomial)
                if not match:
                    return mapping.PlantFacts(
                        hints=[f"USDA: no exact match for '{binomial}'"],
                        candidates=mapping.usda_candidates(results or []),
                    )
                symbol = match.get("Symbol")

            profile = self._fetch("PlantProfile", {"symbol": symbol})
            if isinstance(profile, list):  # some deployments wrap the profile in a list
                profile = profile[0] if profile else {}
            plant_id = profile.get("Id")
            ref = f"{self._base}/PlantProfile?symbol={symbol}"
            if not plant_id:
                return mapping.PlantFacts(hints=[f"USDA: profile for {symbol} has no Id"])

            characteristics = self._fetch(f"PlantCharacteristics/{plant_id}") or []
            wildlife = self._fetch(f"PlantWildlife/{plant_id}") or {}
            facts = mapping.map_usda(profile, characteristics, wildlife, ref)
            facts.hints.insert(0, f"USDA matched symbol {symbol}")
            facts.resolved_id = symbol  # cache on the product for the next fetch
            return facts
        except Exception as exc:  # noqa: BLE001 — never raise to the user
            return mapping.PlantFacts(hints=[f"USDA lookup failed: {type(exc).__name__}: {exc}"])
