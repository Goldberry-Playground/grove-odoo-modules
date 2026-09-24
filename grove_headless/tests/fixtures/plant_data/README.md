# plant_data test fixtures (GOL-2383, spec section B)

Recorded so provider tests run with **no live network in CI**.

- `usda_divi5_*.json` — **real** captures from
  `https://plantsservices.sc.egov.usda.gov/api/` on 2026-09-21 for
  *Diospyros virginiana* (symbol `DIVI5`, Id `64536`): the `PlantSearch`
  candidate list (slimmed to the fields the resolver reads, first 5),
  `PlantProfile?symbol=DIVI5` (slimmed), and the full `PlantCharacteristics/64536`
  and `PlantWildlife/64536` bodies.
- `usda_coam3_*.json` — **schema-faithful** *Corylus americana* (American
  hazelnut, symbol `COAM3`) `PlantProfile` / `PlantCharacteristics` /
  `PlantWildlife`, hand-built to exercise the GOL-2542 USDA-fallback path: it
  carries Shade Tolerance / Moisture Use / soil textures / Fruit-Seed period /
  wildlife (the fields Perenual is *preferred* for, now filled by USDA when
  empty) plus Temperature Minimum −33 °F (→ zone 4, source `usda_temp`) and
  Planting Density 700–1700/acre (→ "5–8 ft" spacing, source `usda_density`).
  Matches Josh's QA-191 observation (product 191, American Hazelnut).
- `perenual_ziziphus_jujuba_list.json` — **schema-faithful** `species-list?q=`
  for *Ziziphus jujuba* (jujube) that returns only near relatives (no exact
  binomial match): the GOL-2542 "Perenual failed" path, where the USDA fallback
  values must stand. USDA has no jujube, so Perenual is the only live source and
  it misses.
- `perenual_ficus_carica_*.json` — **schema-faithful** `species-list?q=` and
  `species/details/{id}` bodies for *Ficus carica*, hand-built to the Perenual
  API **v2** shape because the free-tier `PERENUAL_API_KEY` is injected only in
  the container (never the repo/db). Re-capture against a live key when B is
  wired on QA (the mapper reads only documented v2 keys: `hardiness.min/max`,
  `sunlight`, `watering`, `soil`, `growth_rate`, `flowering_season`,
  `harvest_season`, `attracts`, `dimensions`, `scientific_name`).
