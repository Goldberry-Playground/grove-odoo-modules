"""Tests for the plant-fact enrichment providers (GOL-2383, spec section B).

Pure Python — no Odoo DB, no live network. The service modules are loaded by
file path (same pattern as test_shippo_client.py) so their package-relative
imports and the real grove_headless/__init__.py are never touched.
"""

import importlib.util
import json
import os
import sys
import unittest

_HERE = os.path.dirname(__file__)
_SVC = os.path.join(_HERE, "..", "services", "plant_data")
_FX = os.path.join(_HERE, "fixtures", "plant_data")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # register before exec so dataclasses under `from __future__ import
    # annotations` can resolve their own module during class creation.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mapping = _load("grove_plant_mapping", os.path.join(_SVC, "mapping.py"))
usda = _load("grove_plant_usda", os.path.join(_SVC, "usda.py"))
perenual = _load("grove_plant_perenual", os.path.join(_SVC, "perenual.py"))


def _fx(name):
    with open(os.path.join(_FX, name)) as fh:
        return json.load(fh)


class _Resp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


# ── name resolution ──────────────────────────────────────────────────────────


class TestResolveBinomial(unittest.TestCase):
    def test_plain_binomial(self):
        self.assertEqual(mapping.resolve_binomial("Diospyros virginiana"), ("diospyros virginiana", None))

    def test_author_dropped(self):
        self.assertEqual(mapping.resolve_binomial("Diospyros virginiana L."), ("diospyros virginiana", None))

    def test_cultivar_quotes_stripped(self):
        self.assertEqual(mapping.resolve_binomial("Ficus carica 'Chicago Hardy'"), ("ficus carica", None))

    def test_spp_skipped(self):
        b, reason = mapping.resolve_binomial("Rubus spp.")
        self.assertIsNone(b)
        self.assertIn("spp.", reason)

    def test_hybrid_skipped(self):
        b, reason = mapping.resolve_binomial("Prunus hybrid")
        self.assertIsNone(b)
        self.assertIn("hybrid", reason)

    def test_x_hybrid_marker_skipped(self):
        b, reason = mapping.resolve_binomial("Malus x domestica")
        self.assertIsNone(b)

    def test_times_sign_skipped(self):
        b, reason = mapping.resolve_binomial("Malus × domestica")
        self.assertIsNone(b)

    def test_single_token_rejected(self):
        b, reason = mapping.resolve_binomial("Diospyros")
        self.assertIsNone(b)

    def test_blank_rejected(self):
        self.assertEqual(mapping.resolve_binomial("  ")[0], None)
        self.assertEqual(mapping.resolve_binomial(None)[0], None)


class TestUsdaPickExact(unittest.TestCase):
    def setUp(self):
        self.results = _fx("usda_divi5_search.json")

    def test_picks_species_rank_over_varieties(self):
        match = mapping.usda_pick_exact(self.results, "diospyros virginiana")
        self.assertIsNotNone(match)
        self.assertEqual(match["Symbol"], "DIVI5")
        self.assertEqual(match["Rank"], "Species")

    def test_no_match_returns_none(self):
        self.assertIsNone(mapping.usda_pick_exact(self.results, "quercus alba"))

    def test_candidates_listing(self):
        cands = mapping.usda_candidates(self.results)
        self.assertEqual(len(cands), 5)
        self.assertTrue(cands[0].startswith("DIVI5 — diospyros virginiana"))


# ── USDA mapping ──────────────────────────────────────────────────────────────


class TestMapUsda(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.facts = mapping.map_usda(
            _fx("usda_divi5_profile.json"),
            _fx("usda_divi5_characteristics.json"),
            _fx("usda_divi5_wildlife.json"),
            ref="usda://DIVI5",
        )

    def _val(self, name):
        return self.facts.fields[name].value

    def test_sun_low_shade_tolerance_to_full(self):
        self.assertEqual(self._val("grove_sun"), "full")

    def test_layer_tall_tree_is_canopy(self):
        self.assertEqual(self._val("grove_layer"), "canopy")

    def test_mature_size_from_height(self):
        self.assertEqual(self._val("grove_mature_size"), "up to 55 ft")

    def test_soil_textures_and_ph(self):
        soil = self._val("grove_soil")
        self.assertIn("textures", soil)
        self.assertIn("pH 4.7–7.5", soil)

    def test_growth_rate_slow(self):
        self.assertEqual(self._val("grove_growth_rate"), "slow")

    def test_bloom_season(self):
        self.assertEqual(self._val("grove_bloom_season"), "Late Spring")

    def test_harvest_range(self):
        self.assertEqual(self._val("grove_harvest_season"), "Summer–Winter")

    def test_watering_medium_to_moderate(self):
        self.assertEqual(self._val("grove_watering"), "moderate")

    def test_zones_never_from_usda(self):
        self.assertNotIn("grove_zone_min", self.facts.fields)
        self.assertNotIn("grove_zone_max", self.facts.fields)

    def test_wildlife_all_low_yields_nothing(self):
        # DIVI5 wildlife ratings are all "Low" (< Medium) -> no wildlife field
        self.assertNotIn("grove_wildlife", self.facts.fields)

    def test_zone_hint_present(self):
        self.assertTrue(any("zone 4b" in h for h in self.facts.hints))

    def test_spacing_hint_not_a_field(self):
        self.assertNotIn("grove_spacing", self.facts.fields)
        self.assertTrue(any("density" in h.lower() for h in self.facts.hints))

    def test_all_source_usda(self):
        self.assertTrue(all(fv.source == "usda" for fv in self.facts.fields.values()))

    def test_wildlife_medium_rating_included(self):
        wl = {"Food": [{"Source": "x", "TerrestrialBirds": "Medium", "SmallMammals": "High"}], "Cover": []}
        facts = mapping.map_usda({"GrowthHabits": ["Tree"]}, [], wl, "r")
        self.assertIn("grove_wildlife", facts.fields)
        v = facts.fields["grove_wildlife"].value
        self.assertIn("terrestrial birds", v)
        self.assertIn("small mammals", v)


class TestUsdaLayerRules(unittest.TestCase):
    def _layer(self, habits, height):
        chars = [{"PlantCharacteristicName": "Height, Mature (feet)", "PlantCharacteristicValue": str(height)}]
        f = mapping.map_usda({"GrowthHabits": habits}, chars, {}, "r")
        return f.fields.get("grove_layer")

    def test_short_tree_understory(self):
        self.assertEqual(self._layer(["Tree"], 20).value, "understory")

    def test_shrub(self):
        self.assertEqual(self._layer(["Shrub"], 8).value, "shrub")

    def test_vine(self):
        self.assertEqual(self._layer(["Vine"], 0).value, "vine")

    def test_forb_ground(self):
        self.assertEqual(self._layer(["Forb/herb"], 2).value, "ground")


class TestTempToZone(unittest.TestCase):
    def test_known_points(self):
        self.assertEqual(mapping.temp_to_zone(-21), "4b")
        self.assertEqual(mapping.temp_to_zone(-25), "4b")
        self.assertEqual(mapping.temp_to_zone(-20), "5a")
        self.assertEqual(mapping.temp_to_zone(-60), "1a")
        self.assertEqual(mapping.temp_to_zone(-55), "1b")


# ── USDA provider (HTTP) ──────────────────────────────────────────────────────


class TestUsdaProvider(unittest.TestCase):
    def _router(self, calls=None):
        def get(url, params=None, timeout=None):
            if calls is not None:
                calls.append(url)
            if url.endswith("/PlantSearch"):
                return _Resp(_fx("usda_divi5_search.json"))
            if url.endswith("/PlantProfile"):
                return _Resp(_fx("usda_divi5_profile.json"))
            if url.endswith("/PlantCharacteristics/64536"):
                return _Resp(_fx("usda_divi5_characteristics.json"))
            if url.endswith("/PlantWildlife/64536"):
                return _Resp(_fx("usda_divi5_wildlife.json"))
            raise AssertionError(f"unexpected url {url}")

        return get

    def test_happy_path(self):
        prov = usda.USDAProvider(get=self._router())
        facts = prov.lookup("Diospyros virginiana")
        self.assertEqual(facts.resolved_id, "DIVI5")
        self.assertEqual(facts.fields["grove_layer"].value, "canopy")
        self.assertEqual(facts.fields["grove_growth_rate"].value, "slow")

    def test_cached_symbol_skips_search(self):
        calls = []
        prov = usda.USDAProvider(get=self._router(calls))
        prov.lookup("Diospyros virginiana", cached_id="DIVI5")
        self.assertFalse(any(u.endswith("/PlantSearch") for u in calls))

    def test_no_match_returns_candidates(self):
        prov = usda.USDAProvider(get=self._router())
        facts = prov.lookup("Quercus alba")
        self.assertEqual(facts.fields, {})
        self.assertTrue(facts.candidates)

    def test_skip_hybrid(self):
        prov = usda.USDAProvider(get=self._router())
        facts = prov.lookup("Malus x domestica")
        self.assertTrue(any("skipped" in h for h in facts.hints))

    def test_single_retry_then_success(self):
        state = {"n": 0}
        base = self._router()

        def flaky(url, params=None, timeout=None):
            if url.endswith("/PlantSearch") and state["n"] == 0:
                state["n"] += 1
                raise RuntimeError("transient")
            return base(url, params=params, timeout=timeout)

        prov = usda.USDAProvider(get=flaky)
        facts = prov.lookup("Diospyros virginiana")
        self.assertEqual(facts.resolved_id, "DIVI5")

    def test_failure_becomes_hint_not_exception(self):
        def dead(url, params=None, timeout=None):
            raise RuntimeError("network down")

        prov = usda.USDAProvider(get=dead)
        facts = prov.lookup("Diospyros virginiana")
        self.assertEqual(facts.fields, {})
        self.assertTrue(any("failed" in h for h in facts.hints))


# ── Perenual mapping ──────────────────────────────────────────────────────────


class TestMapPerenual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.facts = mapping.map_perenual(_fx("perenual_ficus_carica_details.json"), "perenual://3")

    def _val(self, name):
        return self.facts.fields[name].value

    def test_zones_from_perenual(self):
        self.assertEqual(self._val("grove_zone_min"), 7)
        self.assertEqual(self._val("grove_zone_max"), 10)

    def test_sun_full(self):
        self.assertEqual(self._val("grove_sun"), "full")

    def test_soil_joined(self):
        self.assertEqual(self._val("grove_soil"), "Loamy and sandy")

    def test_growth_rate_high_to_fast(self):
        self.assertEqual(self._val("grove_growth_rate"), "fast")

    def test_harvest_season(self):
        self.assertEqual(self._val("grove_harvest_season"), "Summer")

    def test_watering_average_to_moderate(self):
        self.assertEqual(self._val("grove_watering"), "moderate")

    def test_wildlife_attracts(self):
        self.assertEqual(self._val("grove_wildlife"), "Attracts bees and birds")

    def test_mature_size_from_dimensions(self):
        self.assertEqual(self._val("grove_mature_size"), "10–30 feet")

    def test_empty_flowering_season_not_written(self):
        self.assertNotIn("grove_bloom_season", self.facts.fields)

    def test_layer_never_from_perenual(self):
        self.assertNotIn("grove_layer", self.facts.fields)

    def test_all_source_perenual(self):
        self.assertTrue(all(fv.source == "perenual" for fv in self.facts.fields.values()))


class TestPerenualSunRules(unittest.TestCase):
    def test_part_shade_to_partial(self):
        self.assertEqual(mapping._perenual_sun(["full sun", "part shade"]), "partial")

    def test_only_full(self):
        self.assertEqual(mapping._perenual_sun(["full sun"]), "full")

    def test_only_shade(self):
        self.assertEqual(mapping._perenual_sun(["deep shade"]), "shade")


# ── Perenual provider (HTTP + budget hook) ────────────────────────────────────


class TestPerenualProvider(unittest.TestCase):
    def _router(self, calls=None, status=200):
        def get(url, params=None, timeout=None):
            if calls is not None:
                calls.append(url)
            if url.endswith("/species-list"):
                return _Resp(_fx("perenual_ficus_carica_list.json"), status_code=status)
            if url.endswith("/species/details/3"):
                return _Resp(_fx("perenual_ficus_carica_details.json"), status_code=status)
            raise AssertionError(f"unexpected url {url}")

        return get

    def test_no_key_skips(self):
        prov = perenual.PerenualProvider(get=self._router(), api_key="")
        facts = prov.lookup("Ficus carica")
        self.assertTrue(any("PERENUAL_API_KEY" in h for h in facts.hints))

    def test_happy_path(self):
        prov = perenual.PerenualProvider(get=self._router(), api_key="k")
        facts = prov.lookup("Ficus carica")
        self.assertEqual(facts.resolved_id, 3)
        self.assertEqual(facts.fields["grove_zone_min"].value, 7)

    def test_on_call_counts_two_calls(self):
        n = {"c": 0}
        prov = perenual.PerenualProvider(
            get=self._router(), api_key="k", on_call=lambda: n.__setitem__("c", n["c"] + 1)
        )
        prov.lookup("Ficus carica")
        self.assertEqual(n["c"], 2)

    def test_cached_id_one_call(self):
        n = {"c": 0}
        calls = []
        prov = perenual.PerenualProvider(
            get=self._router(calls), api_key="k", on_call=lambda: n.__setitem__("c", n["c"] + 1)
        )
        prov.lookup("Ficus carica", cached_id=3)
        self.assertEqual(n["c"], 1)
        self.assertFalse(any(u.endswith("/species-list") for u in calls))

    def test_429_raises_rate_limited(self):
        prov = perenual.PerenualProvider(get=self._router(status=429), api_key="k")
        with self.assertRaises(perenual.PerenualRateLimited):
            prov.lookup("Ficus carica")

    def test_no_match_candidates(self):
        def get(url, params=None, timeout=None):
            return _Resp({"data": [{"id": 9, "scientific_name": ["Quercus alba"], "common_name": "white oak"}]})

        prov = perenual.PerenualProvider(get=get, api_key="k")
        facts = prov.lookup("Ficus carica")
        self.assertEqual(facts.fields, {})
        self.assertTrue(facts.candidates)


# ── merge precedence ──────────────────────────────────────────────────────────


class TestMerge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.usda = mapping.map_usda(
            _fx("usda_divi5_profile.json"),
            _fx("usda_divi5_characteristics.json"),
            _fx("usda_divi5_wildlife.json"),
            "usda://DIVI5",
        )
        cls.perenual = mapping.map_perenual(_fx("perenual_ficus_carica_details.json"), "perenual://3")
        cls.m = mapping.merge(cls.usda, cls.perenual)

    def test_zones_from_perenual(self):
        self.assertEqual(self.m.fields["grove_zone_min"].source, "perenual")

    def test_layer_from_usda(self):
        self.assertEqual(self.m.fields["grove_layer"].source, "usda")

    def test_mature_size_usda_wins(self):
        self.assertEqual(self.m.fields["grove_mature_size"].source, "usda")

    def test_soil_perenual_wins(self):
        self.assertEqual(self.m.fields["grove_soil"].source, "perenual")

    def test_growth_rate_usda_wins(self):
        self.assertEqual(self.m.fields["grove_growth_rate"].source, "usda")

    def test_bloom_usda_wins(self):
        self.assertEqual(self.m.fields["grove_bloom_season"].source, "usda")

    def test_harvest_perenual_wins(self):
        self.assertEqual(self.m.fields["grove_harvest_season"].source, "perenual")

    def test_watering_perenual_wins(self):
        self.assertEqual(self.m.fields["grove_watering"].source, "perenual")

    def test_wildlife_perenual_wins(self):
        self.assertEqual(self.m.fields["grove_wildlife"].source, "perenual")

    def test_sun_perenual_wins(self):
        self.assertEqual(self.m.fields["grove_sun"].source, "perenual")

    def test_hints_concatenated(self):
        self.assertTrue(len(self.m.hints) >= len(self.usda.hints))


# ── budget helpers ────────────────────────────────────────────────────────────


class TestBudgetHelpers(unittest.TestCase):
    def test_counter_key(self):
        self.assertEqual(mapping.counter_key("2026-09-21"), "grove_headless.perenual_calls.2026-09-21")

    def test_calls_needed(self):
        self.assertEqual(mapping.calls_needed(has_cached_id=False), 2)
        self.assertEqual(mapping.calls_needed(has_cached_id=True), 1)

    def test_under_budget(self):
        self.assertTrue(mapping.under_budget(used=98, needed=2, budget=100))
        self.assertFalse(mapping.under_budget(used=99, needed=2, budget=100))
        self.assertTrue(mapping.under_budget(used=99, needed=1, budget=100))

    def test_default_budget(self):
        self.assertEqual(mapping.DEFAULT_DAILY_BUDGET, 100)


if __name__ == "__main__":
    unittest.main()
