"""OpenAPI operation inventory, expressed as one config of the generic
structured-spec reader.

No bespoke parsing lives here: OpenAPI is `structured_spec.OPENAPI_SPEC`, so it is
a configuration of the one declarative-contract engine, not a separate adapter.
The recognised methods and the `derive` entrypoint delegate to that engine, which
keeps the promise unchanged -- locations and hashes, never descriptions, examples,
security values or server URLs; no network access, reference resolution, schema
validation or server selection.
"""

from __future__ import annotations

from .structured_spec import OPENAPI_SPEC
from .structured_spec import derive as _derive_spec

METHODS = frozenset(OPENAPI_SPEC["methods"])


def derive(text: str, relative: str, prefix: str = "") -> list[dict]:
    return _derive_spec(text, OPENAPI_SPEC, relative, prefix)
