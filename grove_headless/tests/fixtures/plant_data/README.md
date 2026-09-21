# plant_data test fixtures (GOL-2383, spec section B)

Recorded so provider tests run with **no live network in CI**.

- `usda_divi5_*.json` — **real** captures from
  `https://plantsservices.sc.egov.usda.gov/api/` on 2026-09-21 for
  *Diospyros virginiana* (symbol `DIVI5`, Id `64536`): the `PlantSearch`
  candidate list (slimmed to the fields the resolver reads, first 5),
  `PlantProfile?symbol=DIVI5` (slimmed), and the full `PlantCharacteristics/64536`
  and `PlantWildlife/64536` bodies.
- `perenual_ficus_carica_*.json` — **schema-faithful** `species-list?q=` and
  `species/details/{id}` bodies for *Ficus carica*, hand-built to the Perenual
  API **v2** shape because the free-tier `PERENUAL_API_KEY` is injected only in
  the container (never the repo/db). Re-capture against a live key when B is
  wired on QA (the mapper reads only documented v2 keys: `hardiness.min/max`,
  `sunlight`, `watering`, `soil`, `growth_rate`, `flowering_season`,
  `harvest_season`, `attracts`, `dimensions`, `scientific_name`).
