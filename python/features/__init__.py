from features.base import Feature, FEATURES
from features.distance_to_strike import DistanceToStrike
from features.safety_ratio import SafetyRatio
from features.reversal_capacity import ReversalCapacity
from features.volatility_expansion import VolatilityExpansion
from features.direction_persistence import DirectionPersistence
from features.signed_flow import SignedFlow
from features.volume_acceleration import VolumeAcceleration

# Auto-register all features
FEATURES.extend([
    DistanceToStrike(),
    SafetyRatio(),
    ReversalCapacity(),
    VolatilityExpansion(),
    DirectionPersistence(),
    SignedFlow(),
    VolumeAcceleration(),
])
