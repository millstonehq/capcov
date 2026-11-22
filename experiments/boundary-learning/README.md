# Learning a boundary probe from observations

This is a small companion to CapCov boundary vectors. Record the incumbent
operation at its boundary, learn a hypothesis from those observations, then
replay the same proposed input against another implementation. The vectors
workflow supplies the execution and comparison receipt; this experiment only
generates candidate probes. It cannot establish parity or qualification.

For the general boundary-replay workflow, see the `capcov-vectors` skill and
the repository's vector documentation before adapting this example.

Run the experiment with Python 3 and PHP 8.2.29.

    php date_observations.php | python3 prefix_rules.py
    python3 -m unittest -v test_prefix_rules

The producer runs 9,025 ordered pairs of printable ASCII characters at one
fixed UTC instant. It supplies no single-character cases. The learner fits
one rule family: a constant prefix followed by a variable character yields
the character as output. It requires two distinct supporting operands, lists
observed exceptions, and proposes the prefix alone if its result is unknown.
It never supplies an expected answer for the proposed probe.

The date run is expected to yield one prefix (backslash), 95 witnesses, zero
exceptions and one probe. Query that probe separately on each implementation.
The expected PHP 8.2.29 result is a one-byte NUL, subject to verification in an
environment with that exact runtime. A wrapper may preprocess the same input
and have a different contract. Training examples must belong to one operation
and environment per invocation.

This is a deterministic, single-rule-family induction experiment. The
rule family and the empty-operand strategy are hand-selected. Output is a
hypothesis for differential testing and confers no Capcov qualification.

To transfer the discovered inputs to a spreadsheet date mask, install
PhpSpreadsheet 1.30.7 separately and pipe the same rules into its public API.

    php date_observations.php | python3 prefix_rules.py | php transfer_sheet.php /path/to/vendor/autoload.php

The transfer preserves each supporting operand and the missing-operand probe.
For cell value 45000.5 and mask prefix yyyy it is intended to yield 96 oracle
observations.
Of the 95 single-character suffixes, 92 yield the year followed by the suffix.
The three exceptions are double quote, at sign and underscore. The terminal
escape yields the year alone. Compare candidate implementations against these
observations, including the exceptions.
