"""Perenual provider (free tier, budgeted). GOL-2383 spec section B.

Never called directly by the Fetch button — the button enqueues a
``grove.enrich.job`` and the cron drains it under the daily budget. The key
comes from ``PERENUAL_API_KEY`` in the container env (never the repo/db).

``on_call`` is invoked once immediately before each HTTP call so the budgeted
job can increment its per-UTC-day counter exactly per real call.

HTTP 429 is ambiguous on Perenual's free tier and must be classified by body:

  * a genuine daily-quota exhaustion -> ``PerenualRateLimited`` so the job marks
    the whole day spent and requeues for the next UTC day, and
  * an ``Upgrade Plan`` body (a *permanent* per-species paywall — a paid-plan
    species, not a rate limit) -> ``PerenualPlanGated`` so the job can fail just
    that species without poisoning the rest of the day's queue.
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
    """HTTP 429 — the day's budget is spent (clears at UTC midnight)."""


class PerenualPlanGated(RuntimeError):
    """HTTP 429 with an ``Upgrade Plan`` body — this *species* is behind a paid
    Perenual plan. A permanent per-species paywall, NOT a rate limit: it does
    not clear at UTC midnight and must not exhaust the day's counter.

    ``species_id`` is the resolved Perenual id when known, so the caller can
    cache it and skip the species-list call on a re-press.
    """

    def __init__(self, message, species_id=None):
        super().__init__(message)
        self.species_id = species_id


def _response_body_text(resp) -> str:
    """Best-effort string of a response body, for classifying a 429.

    Real ``requests.Response`` exposes ``.text``; test fakes may only expose
    ``.json()``. Fall back through both so the discriminator works either way.
    """
    text = getattr(resp, "text", None)
    if isinstance(text, str) and text:
        return text
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — a body we cannot parse is simply "not plan-gated"
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        return " ".join(str(v) for v in body.values())
    return str(body)


def _is_plan_gated(resp) -> bool:
    """A 429 whose body references the paid plan is a per-species paywall."""
    body = _response_body_text(resp).lower()
    return "upgrade plan" in body or "subscription-api-pricing" in body


class PerenualProvider:
    def __init__(self, get=requests.get, api_key: str | None = None, base: str = BASE, on_call=None):
        self._get = get
        self._key = api_key if api_key is not None else os.environ.get("PERENUAL_API_KEY")
        self._base = base.rstrip("/")
        self._on_call = on_call

    @property
    def configured(self) -> bool:
        """True when a PERENUAL_API_KEY is available to make live calls.

        The budgeted cron probes this before draining so that, while the key is
        unprovisioned, queued jobs are left untouched (not silently completed)
        and drain for real once the key lands — the "queue now, drain when
        keyed" contract.
        """
        return bool(self._key)

    def _fetch(self, path: str, params: dict):
        if self._on_call:
            self._on_call()
        params = dict(params, key=self._key)
        resp = self._get(f"{self._base}/{path}", params=params, timeout=TIMEOUT)
        if getattr(resp, "status_code", None) == 429:
            if _is_plan_gated(resp):
                raise PerenualPlanGated("Perenual returned HTTP 429 (Upgrade Plan — species is plan-gated)")
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

        try:
            details = self._fetch(f"species/details/{species_id}", {})
        except PerenualPlanGated as exc:
            # attach the id we already resolved so the queue can cache it and
            # skip re-discovering the paywall (a wasted species-list call).
            exc.species_id = species_id
            raise
        ref = f"{self._base}/species/details/{species_id}"
        facts = mapping.map_perenual(details or {}, ref)
        facts.hints.insert(0, f"Perenual matched species id {species_id}")
        facts.resolved_id = species_id
        return facts
