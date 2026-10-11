"""Vectors: prove a rebuild by recording what the incumbent does at its boundary.

A static inventory says what a system declares. A runtime probe says what an
exercise touched. Neither says whether a CANDIDATE rebuilt from the same
requirements behaves the same. Vectors do: record the incumbent's boundary
behaviour once, replay the same inputs against the candidate from the same
starting state, and compare.

The design, fixed here and not per consumer:

* **The unit is a boundary operation.** An HTTP route, a webhook endpoint, a
  scheduled event, a queued job, a command. Nothing finer: an operation is the
  smallest thing a caller can invoke, so it is the smallest thing a rebuild has
  to get right. The consumer supplies its operations through the plugin seam
  (``schema.Operation``); the engine never special-cases a product.
* **The requirement is recorded vectors.** An operation is covered when every
  vector recorded for it passes against the candidate, and at least one exists.
  A vector is one restored snapshot, one or more steps, the responses, and the
  delta of every store on the edge. Nothing is asserted by hand.
* **The stores are on the edge.** The oracle's databases, document stores,
  caches and object stores are snapshotted before and inspected after every
  vector, so a write that lands in the wrong table, or a read that lands in
  any table, is a difference rather than an invisible side effect. Stores the
  consumer DECLARES for an operation are compared; every other touch is carried
  as informational so it can become a finding, never dropped.
* **The templates are fixed.** The cells per access class (``templates``) are
  the design's, not the operation's: a read has permitted / no-permission /
  other-tenant / missing-record / anonymous, and so on. If a case is not in the
  template it is not required; a new case is added to the template, not to one
  operation. A cell the manifest cannot fill is a named gap.
* **Normalization happens at compare time.** Recorded vectors stay raw. The
  versioned policy in ``normalize`` masks volatile values (timestamps, tokens,
  generated ids) on BOTH sides when they are compared, so a policy change never
  forces a re-record. Replay artifact v3 separately binds frozen policy config,
  imported source bytes and loaded normalizer code identity; replay refuses to
  publish if the source file drifts before publication. Recording provenance
  remains the capture-time account. These identities are local disclosures,
  not authenticated execution attestations.
* **Gaps are never faked.** A cell that cannot be driven, a store that cannot
  be inspected, a fault that cannot be injected: each is a named gap carried
  through the artifacts to the rollup. A vector count that silently excludes
  what could not run is the lie this package exists to refuse.

This package is stdlib-only, like the rest of the CI half of capcov. Processes
and sockets are reached through injectable runners so tests use fakes and a
missing external tool fails with a named error rather than a skipped check.

Modules:

* ``schema``     the dataclasses and the two artifact shapes (vectors, replay)
* ``templates``  the fixed cells per access class and the manifest/inputs lookup
* ``normalize``  the compare-time volatility policy, versioned
* ``diff``       store deltas between two inspections; first difference of two
                 recorded results
* ``replay``     the candidate-side producer, bound to exact vector bytes
* ``claims``     strict ingestion into Python/Souffle claims and certificates

The claim adapter deliberately does not close a whole-system census.  It
proves one operation artifact at a time; the consumer must bind the complete,
versioned operation census and require every operation before reporting a
system-wide percentage.

``vectors.json`` and ``replay.json`` contain executable inputs and observed
responses, so writers mark them ``private-evidence`` and mode 0600. They are
not publication artifacts. The claim adapter exports only identities and
digests and treats replay leaves as local assumptions until a separate,
reviewed producer attestation promotes them.
"""
