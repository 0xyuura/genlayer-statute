"""Unit tests for the deterministic layer of Statute.

Statute compiles a natural language policy into a decision table once, under
consensus, and then decides deterministically forever. Everything that decides
anything lives in a module level pure function, so it can all be tested here
with plain CPython. No network, no model.

That placement is deliberate. In an earlier contract the agreement rule lived
inside a closure, which put it out of reach of tests, and a consensus defect
shipped because of it. Nothing that votes hides in a closure any more.

Run with:  python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _stub                                              # noqa: E402
_stub.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contracts"))
import statute as st                                      # noqa: E402


FACTS = "age:int:0:100:10\ntier:enum:bronze,silver,gold\nverified:bool"
OUTCOMES = "approve,review,deny"


def table(rules, default="review"):
    return {"rules": rules, "default": default}


def rule(outcome, *atoms):
    return {"outcome": outcome,
            "atoms": [{"fact": f, "op": o, "value": str(v)} for f, o, v in atoms]}


class Schema(unittest.TestCase):
    def setUp(self):
        self.schema = st.parse_schema(FACTS)

    def test_parses_all_three_kinds(self):
        self.assertEqual([f["kind"] for f in self.schema],
                         ["int", "enum", "bool"])

    def test_int_fact_keeps_range_and_step(self):
        age = self.schema[0]
        self.assertEqual((age["lo"], age["hi"], age["step"]), (0, 100, 10))

    def test_enum_members_are_sorted_and_unique(self):
        self.assertEqual(self.schema[1]["members"], ["bronze", "gold", "silver"])

    def test_rejects_duplicate_fact_names(self):
        with self.assertRaises(st.gl.vm.UserError):
            st.parse_schema("age:int:0:10:1\nage:bool")

    def test_rejects_unknown_kind(self):
        with self.assertRaises(st.gl.vm.UserError):
            st.parse_schema("age:decimal:0:10:1")

    def test_rejects_non_positive_step(self):
        with self.assertRaises(st.gl.vm.UserError):
            st.parse_schema("age:int:0:10:0")

    def test_rejects_empty_enum(self):
        with self.assertRaises(st.gl.vm.UserError):
            st.parse_schema("tier:enum:")

    def test_semicolons_separate_facts_like_newlines(self):
        # a whole schema has to survive travelling as one ABI argument
        self.assertEqual(st.parse_schema(FACTS.replace(chr(10), ";")),
                         self.schema)

    def test_outcomes_must_be_at_least_two(self):
        with self.assertRaises(st.gl.vm.UserError):
            st.parse_outcomes("approve")


class Grid(unittest.TestCase):
    def setUp(self):
        self.schema = st.parse_schema(FACTS)

    def test_size_is_the_product_of_each_fact_domain(self):
        # age 0..100 step 10 gives 11 values, tier gives 3, verified gives 2
        self.assertEqual(st.grid_size(self.schema), 11 * 3 * 2)

    def test_grid_is_deterministic(self):
        self.assertEqual(st.canonical_grid(self.schema),
                         st.canonical_grid(self.schema))

    def test_grid_matches_declared_size(self):
        self.assertEqual(len(st.canonical_grid(self.schema)),
                         st.grid_size(self.schema))

    def test_oversized_schema_is_refused(self):
        big = "\n".join("f{}:int:0:1000:1".format(i) for i in range(3))
        with self.assertRaises(st.gl.vm.UserError):
            st.parse_schema(big)


class Evaluate(unittest.TestCase):
    def setUp(self):
        self.schema = st.parse_schema(FACTS)

    def test_first_matching_rule_wins(self):
        t = table([rule("deny", ("age", "<", 18)),
                   rule("approve", ("tier", "==", "gold"))])
        facts = {"age": 10, "tier": "gold", "verified": True}
        self.assertEqual(st.evaluate_table(t, facts, self.schema), "deny")

    def test_default_applies_when_nothing_matches(self):
        t = table([rule("deny", ("age", "<", 18))], default="approve")
        facts = {"age": 40, "tier": "bronze", "verified": False}
        self.assertEqual(st.evaluate_table(t, facts, self.schema), "approve")

    def test_atoms_are_conjunctive(self):
        t = table([rule("approve", ("age", ">=", 18), ("verified", "==", "true"))])
        self.assertEqual(st.evaluate_table(
            t, {"age": 30, "tier": "bronze", "verified": False}, self.schema),
            "review")
        self.assertEqual(st.evaluate_table(
            t, {"age": 30, "tier": "bronze", "verified": True}, self.schema),
            "approve")

    def test_facts_must_be_complete(self):
        with self.assertRaises(st.gl.vm.UserError):
            st.evaluate_table(table([]), {"age": 30}, self.schema)

    def test_int_fact_out_of_declared_range_is_refused(self):
        with self.assertRaises(st.gl.vm.UserError):
            st.evaluate_table(table([]),
                              {"age": 900, "tier": "gold", "verified": True},
                              self.schema)


class LoadFacts(unittest.TestCase):
    """Callers send facts in more than one shape; all of them must land."""

    def setUp(self):
        self.schema = st.parse_schema(FACTS)
        self.want = {"age": 30, "tier": "gold", "verified": True}

    def test_accepts_a_json_string(self):
        self.assertEqual(
            st._load_facts('{"age":30,"tier":"gold","verified":true}',
                           self.schema), self.want)

    def test_accepts_an_already_decoded_mapping(self):
        # the CLI decodes a {...} argument before it reaches the contract
        self.assertEqual(st._load_facts(dict(self.want), self.schema), self.want)

    def test_accepts_utf8_bytes(self):
        self.assertEqual(
            st._load_facts(b'{"age":30,"tier":"gold","verified":true}',
                           self.schema), self.want)

    def test_still_refuses_junk(self):
        with self.assertRaises(st.gl.vm.UserError):
            st._load_facts("not json", self.schema)

    def test_still_validates_values(self):
        with self.assertRaises(st.gl.vm.UserError):
            st._load_facts({"age": 30, "tier": "platinum", "verified": True},
                           self.schema)


class ParseTable(unittest.TestCase):
    def setUp(self):
        self.schema = st.parse_schema(FACTS)
        self.outcomes = st.parse_outcomes(OUTCOMES)

    def ok(self, raw):
        return st.parse_table(raw, self.schema, self.outcomes)

    def bad(self, raw):
        with self.assertRaises(st.gl.vm.UserError):
            st.parse_table(raw, self.schema, self.outcomes)

    def test_accepts_a_well_formed_table(self):
        t = self.ok(table([rule("deny", ("age", "<", 18))]))
        self.assertEqual(len(t["rules"]), 1)

    def test_rejects_unknown_fact(self):
        self.bad(table([rule("deny", ("height", "<", 18))]))

    def test_rejects_unknown_operator(self):
        self.bad(table([rule("deny", ("age", "~=", 18))]))

    def test_rejects_ordering_operator_on_enum(self):
        self.bad(table([rule("deny", ("tier", "<", "gold"))]))

    def test_rejects_enum_value_outside_members(self):
        self.bad(table([rule("deny", ("tier", "==", "platinum"))]))

    def test_rejects_outcome_outside_declared_set(self):
        self.bad(table([rule("escalate", ("age", "<", 18))]))

    def test_rejects_missing_default(self):
        self.bad({"rules": []})

    def test_rejects_default_outside_declared_set(self):
        self.bad(table([], default="escalate"))

    def test_rejects_rule_with_no_atoms(self):
        self.bad(table([{"outcome": "deny", "atoms": []}]))

    def test_rejects_non_integer_value_on_int_fact(self):
        self.bad(table([rule("deny", ("age", "<", "eighteen"))]))

    def test_is_order_preserving(self):
        t = self.ok(table([rule("deny", ("age", "<", 18)),
                           rule("approve", ("age", ">=", 18))]))
        self.assertEqual([r["outcome"] for r in t["rules"]], ["deny", "approve"])


class Agreement(unittest.TestCase):
    """The core of the primitive: validators agree on behaviour, not on text."""

    def setUp(self):
        self.schema = st.parse_schema(FACTS)

    def agree(self, a, b):
        return st.tables_agree(a, b, self.schema,
                               st.boundary_probes(self.schema, [a, b]))

    def test_same_table_agrees_with_itself(self):
        t = table([rule("deny", ("age", "<", 18))])
        self.assertTrue(self.agree(t, t))

    def test_textually_different_but_behaviourally_identical_tables_agree(self):
        mine = table([rule("deny", ("age", "<", 18))], default="approve")
        theirs = table([rule("approve", ("age", ">=", 18))], default="deny")
        self.assertTrue(self.agree(mine, theirs))

    def test_split_rules_covering_the_same_region_agree(self):
        mine = table([rule("deny", ("tier", "!=", "gold"))], default="approve")
        theirs = table([rule("deny", ("tier", "==", "bronze")),
                        rule("deny", ("tier", "==", "silver"))],
                       default="approve")
        self.assertTrue(self.agree(mine, theirs))

    def test_a_single_boundary_disagreement_is_caught(self):
        mine = table([rule("deny", ("age", "<", 18))], default="approve")
        theirs = table([rule("deny", ("age", "<=", 18))], default="approve")
        self.assertFalse(self.agree(mine, theirs))

    def test_different_default_is_caught(self):
        mine = table([rule("deny", ("age", "<", 18))], default="approve")
        theirs = table([rule("deny", ("age", "<", 18))], default="review")
        self.assertFalse(self.agree(mine, theirs))

    def test_agreement_is_symmetric(self):
        cuts = [0, 18, 21, 65, 100]
        built = [table([rule("deny", ("age", "<", c))], default="approve")
                 for c in cuts]
        checked = 0
        for a in built:
            for b in built:
                probes = st.boundary_probes(self.schema, [a, b])
                self.assertEqual(st.tables_agree(a, b, self.schema, probes),
                                 st.tables_agree(b, a, self.schema, probes))
                checked += 1
        self.assertEqual(checked, len(cuts) ** 2)

    def test_no_agreeing_pair_can_decide_differently_on_the_canonical_grid(self):
        """The property that matters: agreement implies identical decisions."""
        built = []
        for c in [0, 10, 18, 40, 100]:
            for ti in ["bronze", "silver", "gold"]:
                built.append(table([rule("deny", ("age", "<", c)),
                                    rule("approve", ("tier", "==", ti))],
                                   default="review"))
        grid = st.canonical_grid(self.schema)
        agreed = 0
        for a in built:
            for b in built:
                probes = st.boundary_probes(self.schema, [a, b])
                if st.tables_agree(a, b, self.schema, probes):
                    for facts in grid:
                        self.assertEqual(
                            st.evaluate_table(a, facts, self.schema),
                            st.evaluate_table(b, facts, self.schema),
                            "agreed tables decided differently at %r" % (facts,))
                    agreed += 1
        self.assertGreater(agreed, 0)


class DeadRules(unittest.TestCase):
    def setUp(self):
        self.schema = st.parse_schema(FACTS)

    def test_a_shadowed_rule_is_reported(self):
        t = table([rule("deny", ("age", "<", 30)),
                   rule("approve", ("age", "<", 20))])
        probes = st.boundary_probes(self.schema, [t])
        self.assertEqual(st.dead_rules(t, self.schema, probes), [1])

    def test_a_live_rule_is_not_reported(self):
        t = table([rule("deny", ("age", "<", 20)),
                   rule("approve", ("age", "<", 30))])
        probes = st.boundary_probes(self.schema, [t])
        self.assertEqual(st.dead_rules(t, self.schema, probes), [])

    def test_a_table_with_a_dead_rule_is_refused_by_agreement(self):
        live = table([rule("deny", ("age", "<", 30))])
        dead = table([rule("deny", ("age", "<", 30)),
                      rule("deny", ("age", "<", 20))])
        probes = st.boundary_probes(self.schema, [live, dead])
        self.assertFalse(st.tables_agree(live, dead, self.schema, probes))


class Fingerprint(unittest.TestCase):
    def setUp(self):
        self.schema = st.parse_schema(FACTS)

    def test_is_stable(self):
        t = table([rule("deny", ("age", "<", 18))])
        self.assertEqual(st.fingerprint(t, self.schema),
                         st.fingerprint(t, self.schema))

    def test_ignores_wording_and_tracks_behaviour(self):
        mine = table([rule("deny", ("age", "<", 18))], default="approve")
        theirs = table([rule("approve", ("age", ">=", 18))], default="deny")
        self.assertEqual(st.fingerprint(mine, self.schema),
                         st.fingerprint(theirs, self.schema))

    def test_changes_when_behaviour_changes(self):
        a = table([rule("deny", ("age", "<", 20))], default="approve")
        b = table([rule("deny", ("age", "<", 30))], default="approve")
        self.assertNotEqual(st.fingerprint(a, self.schema),
                            st.fingerprint(b, self.schema))

    def test_resolution_below_the_declared_step_is_invisible(self):
        """A deliberate, documented limit, pinned so it cannot drift.

        The fingerprint is behaviour on the canonical grid, and the grid is
        the schema's own declared step. With age declared at step 10, a
        threshold at 18 and a threshold at 19 decide identically at every
        grid point, so they share a fingerprint and `diff` reports nothing.
        Declare a finer step when a policy really turns on single units.

        Consensus is unaffected: agreement runs on boundary probes, which do
        carry c-1, c, c+1, so the two tables below still fail to agree.
        """
        a = table([rule("deny", ("age", "<", 18))], default="approve")
        b = table([rule("deny", ("age", "<", 19))], default="approve")
        self.assertEqual(st.fingerprint(a, self.schema),
                         st.fingerprint(b, self.schema))
        self.assertEqual(st.diff_points(a, b, self.schema, 50), [])
        probes = st.boundary_probes(self.schema, [a, b])
        self.assertFalse(st.tables_agree(a, b, self.schema, probes))


class Diff(unittest.TestCase):
    def setUp(self):
        self.schema = st.parse_schema(FACTS)

    def test_identical_behaviour_has_no_differences(self):
        t = table([rule("deny", ("age", "<", 18))], default="approve")
        self.assertEqual(st.diff_points(t, t, self.schema, 50), [])

    def test_reports_exactly_the_points_that_changed(self):
        a = table([rule("deny", ("age", "<", 20))], default="approve")
        b = table([rule("deny", ("age", "<", 10))], default="approve")
        pts = st.diff_points(a, b, self.schema, 200)
        self.assertTrue(pts)
        self.assertTrue(all(p["facts"]["age"] == 10 for p in pts))
        self.assertEqual(len(pts), 3 * 2)
        self.assertEqual(pts[0]["from"], "deny")
        self.assertEqual(pts[0]["to"], "approve")


if __name__ == "__main__":
    unittest.main()
