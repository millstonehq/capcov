"""Probes: the reality side. Runtime observers behind one registry, one contract.

A probe runs a system's own exercises and reports what they ACTUALLY touched,
writing a single ``observed`` artifact the one reconcile consumes. Three ship:

* ``pytest``  -- the default. Three hooks and a context variable: a data hook,
  which every ORM has; a request hook, which every web framework has; and a
  context variable correlating them, which is the part that turns two independent
  streams into a BINDING rather than two coverage numbers.
* ``browser`` -- drives a planned scenario through a real browser and projects the
  requests it made onto the same bindings.
* ``load``    -- drives a headless ELT pipeline and projects the stages, tables
  and sinks it moved onto the same bindings (a contract stub for now).

What every probe emits is route templates, table names and effects -- no
parameters, no rows, no values. That is what makes this something a third party
will agree to run against a system you do not own: they can read the whole output
file before they send it back. Each probe also carries its own honest-denominator
``excluded_surfaces`` / ``unresolved`` so what it saw-but-filtered and
could-not-resolve reaches the gate rather than shrinking the denominator silently.

``probe_registry`` is the sibling of ``capcov.adapters``: adapters name the
declared (static) readers, probes name the actual (runtime) observers.
"""

from .probe_registry import (  # noqa: F401
    EMPTY_EXCLUDED_SURFACES,
    ENV_NONCE,
    ENV_ONLY,
    ENV_OUT,
    ENV_SOURCE_ROOT,
    ENV_TARGET,
    REGISTRY,
    FreshnessGuard,
    excluded_surfaces_carrier,
    load,
    observed_carriers,
    resolve,
)

__all__ = [
    "EMPTY_EXCLUDED_SURFACES",
    "ENV_NONCE",
    "ENV_ONLY",
    "ENV_OUT",
    "ENV_SOURCE_ROOT",
    "ENV_TARGET",
    "REGISTRY",
    "FreshnessGuard",
    "excluded_surfaces_carrier",
    "load",
    "observed_carriers",
    "resolve",
]
