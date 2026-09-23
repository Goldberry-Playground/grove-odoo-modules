"""Plant-fact enrichment providers (USDA PLANTS + Perenual). GOL-2383 section B.

Public surface — the ``Provider.lookup(botanical_name, cached_id=None) ->
PlantFacts`` interface plus the pure mapping/budget helpers the enrich job wraps.
"""

from . import mapping
from .mapping import (
    DEFAULT_DAILY_BUDGET,
    FIELD_PRECEDENCE,
    FactValue,
    PlantFacts,
    calls_needed,
    counter_key,
    merge,
    resolve_binomial,
    under_budget,
)
from .perenual import PerenualPlanGated, PerenualProvider, PerenualRateLimited
from .usda import USDAProvider

__all__ = [
    "mapping",
    "FactValue",
    "PlantFacts",
    "FIELD_PRECEDENCE",
    "DEFAULT_DAILY_BUDGET",
    "resolve_binomial",
    "merge",
    "counter_key",
    "calls_needed",
    "under_budget",
    "USDAProvider",
    "PerenualProvider",
    "PerenualRateLimited",
    "PerenualPlanGated",
]
