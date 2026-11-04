"""Intake-only catalog additions; preserve the legacy agent's routing."""
from ..home_guide_tools import KNOWN_SERVICE_TYPES as LEGACY_SERVICE_TYPES

KNOWN_SERVICE_TYPES = [*LEGACY_SERVICE_TYPES, 'Landscaping']
