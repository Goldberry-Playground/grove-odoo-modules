"""Perenual provider (free tier, budgeted). GOL-2383 spec section B.

Never called directly by the Fetch button — the button enqueues a
``grove.enrich.job`` and the cron drains it under the daily budget. The key
comes from ``PERENUAL_API_KEY`` in the container env (never the repo/db).

``on_call`` is invoked once immediately before each HTTP call so the budgeted
job can increment its per-UTC-day counter exactly per real call. HTTP 429 is
raised as ``PerenualRateLimited`` so the job can mark the day exhausted.
"""

from __future__ import annotations

import os

import requests

try:
    from . import mapping
except ImportError:
    import sys as _sys

    mapping = _sys.modules.get("grove_plant_mapping")
    if mapping is None:
        import importlib.util as _ilu

        _p = os.path.join(os.path.dirname(__file__), "mapping.py")
        _spec = _ilu.spec_from_file_location("grove_plant_mapping", _p)
        mapping = _ilu.module_from_spec(_spec)
        _sys.modules["grove_plant_mapping"] = mapping  # register before exec (dataclasses)
        _spec.loader.exec_module(mapping)

BASE = "https://perenual.com/api/v2"
TIMEOUT = 10


class PerenualRateLimited(RuntimeError):
    """HTTP 429 — the day's budget is spent."""


class PerenualProvider:
    def __init__(self, get=requests.get, api_key: str | None = None, base: str = BASE, on_call=None):
        self._get = get
        self._key = api_key if api_key is not None else os.environ.get("PERENUAL_API_KEY")
        self._base = base.rstrip("/")
        self._on_call = on_call

    def _fetch(self, path: str, params: dict):
        if self._on_call:
            self._on_call()
        params = dict(params, key=self._key)
        resp = self._get(f"{self._base}/{path}", params=params, timeout=TIMEOUT)
        if getattr(resp, "status_code", None) == 429:
            raise PerenualRateLimited("Perenual returned HTTP 429 (daily budget spent)")
        resp.raise_for_status()
        return resp.json()

    def lookup(self, botanical_name, cached_id=None) -> "mapping.PlantFacts":
        if not self._key:
            return mapping.PlantFacts(hints=["Perenual skipped: PERENUAL_API_KEY not set"])
        binomial, skip = mapping.resolve_binomial(botanical_name)
        if skip:
            return mapping.PlantFacts(hints=[f"Perenual skipped: {skip}"])

        species_id = cached_id
        if not species_id:
            payload = self._fetch("species-list", {"q": binomial})
            results = (payload or {}).get("data", []) if isinstance(payload, dict) else (payload or [])
            match = mapping.perenual_pick_exact(results, binomial)
            if not match:
                return mapping.PlantFacts(
                    hints=[f"Perenual: no exact match for '{binomial}'"],
                    candidates=mapping.perenual_candidates(results),
                )
            species_id = match.get("id")

        details = self._fetch(f"species/details/{species_id}", {})
        ref = f"{self._base}/species/details/{species_id}"
        facts = mapping.map_perenual(details or {}, ref)
        facts.hints.insert(0, f"Perenual matched species id {species_id}")
        facts.resolved_id = species_id
        return facts
