"""Relation declarations and join rules for runtime causal-trace evidence.

The causal-trace primitives ``runtime_function_entered``, ``runtime_sql_executed``,
``runtime_tx_committed`` and ``runtime_route_completed`` declare the producer class
``target-go-runtime-trace-v2``; evidence whose ``source`` token is anything else is
rejected at ingestion (``evidence-producer``) and again by the ground checker.  A
consumer that retains runtime receipts supplies that producer; capcov ships the
declarations and the rules that join a run to an index.

Static and runtime certificates stay independent: ``index_describes_run`` joins a
run to an index, and ``runtime_route_reaches_sql_on_index`` requires agreeing
run/request/transaction/surface/index witnesses.  That is not one end-to-end proof
that a static call-graph path was the runtime stack.
"""
from __future__ import annotations

from ..ir import Atom, Column, RelationDecl, Rule, Variable

TRACE_PRODUCER = "target-go-runtime-trace-v2"

RUNTIME_ROUTE_OBSERVED = RelationDecl(
    "runtime_route_observed",
    (Column("tenant", "symbol", True), Column("surface", "symbol", True),
     Column("event", "symbol", True), Column("run", "symbol", True)),
    context_indices=("tenant", "surface", "event", "run"))

TRACE_PRIMITIVE_DECLS = (
    RelationDecl("runtime_function_entered",
                 (Column("run", "symbol", True), Column("request", "symbol", True),
                  Column("symbol", "symbol")),
                 producer_classes=(TRACE_PRODUCER,), context_indices=("run", "request")),
    RelationDecl("runtime_sql_executed",
                 (Column("run", "symbol", True), Column("request", "symbol", True),
                  Column("tx", "symbol", True), Column("operation", "symbol"),
                  Column("ordinal", "unsigned")),
                 producer_classes=(TRACE_PRODUCER,), context_indices=("run", "request", "tx")),
    RelationDecl("runtime_tx_committed",
                 (Column("run", "symbol", True), Column("request", "symbol", True),
                  Column("tx", "symbol", True)),
                 producer_classes=(TRACE_PRODUCER,), context_indices=("run", "request", "tx")),
    RelationDecl("runtime_route_completed",
                 (Column("run", "symbol", True), Column("request", "symbol", True),
                  Column("surface", "symbol", True)),
                 producer_classes=(TRACE_PRODUCER,), context_indices=("run", "request", "surface")),
)

RUNTIME_ROUTE_REACHES_SQL = RelationDecl(
    "runtime_route_reaches_sql_on_index",
    (Column("index", "digest", True), Column("run", "symbol", True),
     Column("request", "symbol", True), Column("surface", "symbol", True),
     Column("symbol", "symbol"), Column("tx", "symbol", True),
     Column("operation", "symbol")),
    modality="derived", binding="runtime", primitive=False,
    context_indices=("index", "run", "request", "surface", "tx"))

RUNTIME_ROUTE_ON_INDEX = RelationDecl(
    "runtime_route_observed_on_index",
    (Column("index", "digest", True), Column("tenant", "symbol", True),
     Column("surface", "symbol", True), Column("run", "symbol", True)),
    modality="derived", binding="runtime", primitive=False,
    context_indices=("index", "tenant", "surface", "run"))

RUNTIME_ROUTE_ON_INDEX_RULE = Rule(
    Atom("runtime_route_observed_on_index",
         (Variable("IX"), Variable("T"), Variable("S"), Variable("Run"))),
    (Atom("runtime_route_observed", (Variable("T"), Variable("S"), Variable("Request"), Variable("Run"))),
     Atom("index_describes_run", (Variable("IX"), Variable("Run"))),
     Atom("scip_index", (Variable("IX"), Variable("Indexer"), Variable("Version"),
                         Variable("Language"), Variable("Root"), Variable("Kind")))),
    name="runtime_route_matches_scip_index")

RUNTIME_ROUTE_REACHES_SQL_RULE = Rule(
    Atom("runtime_route_reaches_sql_on_index",
         (Variable("IX"), Variable("Run"), Variable("Request"), Variable("Surface"),
          Variable("Symbol"), Variable("Tx"), Variable("Operation"))),
    (Atom("runtime_route_observed", (Variable("Tenant"), Variable("Surface"),
                                    Variable("Request"), Variable("Run"))),
     Atom("runtime_function_entered", (Variable("Run"), Variable("Request"), Variable("Symbol"))),
     Atom("runtime_sql_executed", (Variable("Run"), Variable("Request"), Variable("Tx"),
                                  Variable("Operation"), Variable("Ordinal"))),
     Atom("runtime_tx_committed", (Variable("Run"), Variable("Request"), Variable("Tx"))),
     Atom("runtime_route_completed", (Variable("Run"), Variable("Request"), Variable("Surface"))),
     Atom("index_describes_run", (Variable("IX"), Variable("Run"))),
     Atom("scip_index", (Variable("IX"), Variable("Indexer"), Variable("Version"),
                         Variable("Language"), Variable("Root"), Variable("Kind")))),
    name="runtime_route_executes_committed_sql_on_index")
