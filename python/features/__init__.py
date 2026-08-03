from features.base import Feature, FEATURES
from features.distance_to_strike import DistanceToStrike
from features.safety_ratio import SafetyRatio
from features.reversal_capacity import ReversalCapacity
from features.volatility_expansion import VolatilityExpansion
from features.direction_persistence import DirectionPersistence
from features.cumulative_buy_pct import CumulativeBuyPct
from features.imbalance_trend import ImbalanceTrend

# Auto-register all features (7 total)
# Indices: 0=distance_to_strike, 1=safety_ratio, 2=reversal_capacity,
#          3=volatility_expansion, 4=direction_persistence, 5=cumulative_buy_pct,
#          6=imbalance_trend
FEATURES.extend([
    DistanceToStrike(),
    SafetyRatio(),
    ReversalCapacity(),
    VolatilityExpansion(),
    DirectionPersistence(),
    CumulativeBuyPct(),
    ImbalanceTrend(),
])
