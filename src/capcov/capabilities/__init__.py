"""Reconcile what a system is specified to do, what it implements, and what it did.

Three sets of capability IDs, each read by an adapter:

    specified    `spec.read_openspec`: requirements and scenarios of an OpenSpec tree
    implemented  `implemented.read`: discovery surfaces mapped to IDs by configuration
    exercised    `exercised.read`: records parsed from structured logs, pass or fail

`reconcile.reconcile` compares them and `reconcile.gate` applies explicit
exclusions. An ID is `<area>/<requirement>/<scenario>`; see `ids`.
"""
