"""
Importing this package registers every deterministic check.

Each check module below registers itself via the `@register` decorator
as a side effect of being imported. Without this file explicitly
importing them, the registry (`all_checks()`) would silently depend on
something ELSE having imported these modules first — which worked by
accident when pytest collected other test files first, but is not
something to rely on (a production entrypoint that only imports
`GeneratorAgent`, for instance, would otherwise see an empty registry
and silently run zero checks).
"""

from app.checks import (  # noqa: F401
    completeness,
    consistency,
    injection_detection,
    referential_integrity,
    type_consistency,
    uniqueness,
    validity,
)
