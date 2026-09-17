"""Lane A and Lane B share a process and must never share a claim.

``observations_agree`` says two implementations were observed to answer alike on
an admitted scenario set.  ``op_qualified`` says something much stronger.  The
whole design of the observation pack rests on those two never being joinable,
and the residual risk the spec records (R10) is social, not structural: a
summary that abbreviates the verdict to "parity".  What CAN be checked
structurally is checked here.

Three separations, by name:

* the two packs combine without a duplicate declaration, so nothing in Lane A
  silently redefines a Lane B relation (or the reverse);
* no rule in the combined pack mentions both an ``observation_*`` relation and
  an ``op_qualified*`` relation -- there is no join between the lanes, not even
  a latent one;
* the observation claim is keyed on ``(run, check, scenario_set, policy)`` and
  carries no op and no index column, which is what makes the two claim rows
  structurally unjoinable rather than merely differently named.
"""
from __future__ import annotations

import unittest

from capcov.claims import validate_bundle
from capcov.claims.static.combine import combine
from capcov.claims.observation.pack import DIAGNOSTIC_POLICY as OBSERVATION_POLICY
from capcov.claims.observation.pack import load_pack as load_observation_pack
from capcov.claims.observation.pack import pack_bundle as observation_pack
from capcov.claims.replay.pack import DIAGNOSTIC_POLICY as REPLAY_POLICY
from capcov.claims.replay.pack import load_pack as load_replay_pack
from capcov.claims.replay.pack import pack_bundle as replay_pack

OBSERVATION_PREFIX = "observation_"
QUALIFIED_PREFIX = "op_qualified"


def _names(rule: dict) -> set[str]:
    """Every relation a rule names.  Comparison atoms (``{"comparison": ...}``)
    name no relation and are skipped; the replay pack has sixteen of them."""
    return {atom["relation"] for atom in (rule["head"], *rule["body"]) if "relation" in atom}


class LaneSeparationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.observation = load_observation_pack()
        cls.replay = load_replay_pack()
        cls.observation_bundle = observation_pack()
        cls.replay_bundle = replay_pack()

    def test_both_packs_validate_on_their_own(self) -> None:
        self.assertEqual(validate_bundle(self.observation_bundle), ())
        self.assertEqual(validate_bundle(self.replay_bundle), ())

    def test_the_packs_share_a_diagnostic_policy_so_they_can_be_combined(self) -> None:
        self.assertEqual(OBSERVATION_POLICY, REPLAY_POLICY)

    def test_loading_both_packs_together_raises_no_duplicate_declaration(self) -> None:
        combined = combine(self.observation_bundle, self.replay_bundle, validate=False)
        self.assertEqual(len(combined.relations),
                         len(self.observation_bundle.relations) + len(self.replay_bundle.relations))
        self.assertEqual(len(combined.rules),
                         len(self.observation_bundle.rules) + len(self.replay_bundle.rules))
        names = [decl.name for decl in combined.relations]
        self.assertEqual(len(names), len(set(names)))

    def test_no_relation_name_is_declared_by_both_packs(self) -> None:
        observation = {decl.name for decl in self.observation_bundle.relations}
        replay = {decl.name for decl in self.replay_bundle.relations}
        self.assertEqual(sorted(observation & replay), [])

    def test_no_rule_mixes_an_observation_relation_with_a_qualified_relation(self) -> None:
        for pack in (self.observation, self.replay):
            for rule in pack["rules"]:
                names = _names(rule)
                observation = {name for name in names if name.startswith(OBSERVATION_PREFIX)}
                qualified = {name for name in names if name.startswith(QUALIFIED_PREFIX)}
                self.assertFalse(observation and qualified,
                                 f"rule {rule['name']} joins {sorted(observation)} to "
                                 f"{sorted(qualified)}")

    def test_the_observation_pack_declares_no_op_or_index_column(self) -> None:
        for relation in (*self.observation["primitives"], *self.observation["derived"]):
            for column in relation["columns"]:
                self.assertNotIn(column["name"], ("op", "index", "model", "mutant"),
                                 f"{relation['name']}.{column['name']}")

    def test_the_two_claim_heads_have_no_shared_key(self) -> None:
        """The arity decision of conflict C2, checked rather than asserted."""
        observation = next(relation for relation in self.observation["derived"]
                           if relation["name"] == "observations_agree")
        qualified = next(relation for relation in self.replay["derived"]
                         if relation["name"] == "op_qualified")
        observation_columns = {column["name"] for column in observation["columns"]}
        qualified_columns = {column["name"] for column in qualified["columns"]}
        self.assertEqual(observation_columns, {"run", "check", "scenario_set", "policy"})
        self.assertEqual(qualified_columns, {"index", "run", "op"})
        # ``run`` is the ONE column the two heads share, and it is the one the
        # spec's conflict C2 keeps: a Lane A run and a Lane B run are different
        # runs with different receipts.  What matters is that neither of the
        # columns that would make a join meaningful -- ``op`` and ``index`` --
        # is on this head, so there is no key to join the two rows ON.
        self.assertEqual(observation_columns & qualified_columns, {"run"})
        self.assertNotIn("op", observation_columns)
        self.assertNotIn("index", observation_columns)

    def test_the_observation_pack_declares_no_lane_b_producer_class(self) -> None:
        observation_classes: set[str] = set()
        for relation in self.observation["primitives"]:
            observation_classes.update(relation["producer_classes"])
        replay_classes: set[str] = set()
        for relation in self.replay["primitives"]:
            replay_classes.update(relation["producer_classes"])
        self.assertEqual(observation_classes, {"observe", "reviewer"})
        # A producer Lane B knows about cannot sign a Lane A row.
        self.assertEqual(observation_classes & (replay_classes - {"reviewer"}), set())


if __name__ == "__main__":
    unittest.main()
