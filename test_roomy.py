"""Tests for roomy.

The invariants worth guarding are not "does it count tokens". They are:

  1. the budget is NEVER exceeded, including the reserve
  2. every candidate gets a decision - nothing is dropped silently
  3. the result does not depend on the order the caller built the list in
  4. a required item that does not fit RAISES, it does not quietly vanish
"""

import unittest

from roomy import (Candidate, Plan, WontFit, estimate_tokens, pack, read_dir,
                   SKIP_DIRS, TEXT_EXT)


def c(name, size, priority=0, required=False):
    """A candidate whose token count is close to `size`."""
    return Candidate(name=name, text=("word " * size).strip(),
                     priority=priority, required=required)


class TestEstimate(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_monotone_in_length(self):
        prev = -1
        for n in (1, 5, 50, 500):
            t = estimate_tokens("word " * n)
            self.assertGreater(t, prev)
            prev = t

    def test_punctuation_counts(self):
        self.assertGreater(estimate_tokens("a,b,c"), estimate_tokens("abc"))

    def test_long_identifiers_cost_more_than_their_length_suggests(self):
        # four short words against one long identifier of the same characters
        short = estimate_tokens("aaa bbb ccc ddd")
        long_ = estimate_tokens("aaabbbcccddd")
        self.assertGreater(short, long_)
        # but the long one is still more than one token
        self.assertGreater(long_, 1)

    def test_newlines_are_counted(self):
        self.assertGreater(estimate_tokens("a\nb\nc"), estimate_tokens("a b c") - 3)

    def test_conservative_against_four_chars_per_token(self):
        # the rule of thumb everyone quotes, on code - roomy must not be LOWER
        code = "def compute_the_thing(self, other_thing):\n    return other_thing + 1\n" * 20
        self.assertGreaterEqual(estimate_tokens(code), len(code) // 4)


class TestPackBasics(unittest.TestCase):
    def test_everything_fits(self):
        p = pack([c("a", 10), c("b", 10)], budget=1000)
        self.assertEqual(len(p.included), 2)
        self.assertEqual(p.excluded, [])

    def test_every_candidate_gets_a_decision(self):
        cands = [c("a", 100), c("b", 100), c("c", 100), c("d", 100)]
        p = pack(cands, budget=250)
        self.assertEqual(len(p.decisions), len(cands))
        self.assertEqual({d.name for d in p.decisions}, {x.name for x in cands})

    def test_every_exclusion_has_a_reason(self):
        p = pack([c("a", 200), c("b", 200), c("c", 200)], budget=250)
        self.assertTrue(p.excluded)
        for d in p.excluded:
            self.assertTrue(d.reason.strip())

    def test_budget_is_never_exceeded(self):
        for budget in (50, 120, 333, 1000, 5000):
            cands = [c("f%02d" % i, 7 * (i + 1)) for i in range(20)]
            p = pack(cands, budget=budget)
            self.assertLessEqual(p.used, budget)

    def test_reserve_is_held_back(self):
        cands = [c("f%02d" % i, 20) for i in range(20)]
        p = pack(cands, budget=500, reserve=200)
        self.assertLessEqual(p.used, 300)
        self.assertEqual(p.headroom, 300 - p.used)

    def test_reserve_actually_changes_the_outcome(self):
        cands = [c("f%02d" % i, 20) for i in range(20)]
        wide = pack(cands, budget=500, reserve=0)
        tight = pack(cands, budget=500, reserve=400)
        self.assertGreater(len(wide.included), len(tight.included))


class TestOrdering(unittest.TestCase):
    def test_priority_wins(self):
        p = pack([c("low", 100, priority=0), c("high", 100, priority=5)], budget=150)
        self.assertEqual(p.names(), ["high"])

    def test_smaller_first_within_a_priority(self):
        p = pack([c("big", 100), c("small", 10)], budget=150)
        self.assertEqual(p.names()[0], "small")

    def test_result_is_independent_of_input_order(self):
        cands = [c("a", 30), c("b", 10, priority=1), c("c", 30), c("d", 10)]
        a = pack(cands, budget=60).names()
        b = pack(list(reversed(cands)), budget=60).names()
        self.assertEqual(a, b)

    def test_ties_break_on_name_not_position(self):
        cands = [c("zebra", 50), c("apple", 50)]
        p = pack(cands, budget=50)
        self.assertEqual(p.names(), ["apple"])
        self.assertEqual(pack(list(reversed(cands)), budget=50).names(), ["apple"])

    def test_packing_is_deterministic(self):
        cands = [c("f%02d" % i, (i * 13) % 50 + 1, priority=i % 3) for i in range(30)]
        first = pack(cands, budget=200).names()
        for _ in range(5):
            self.assertEqual(pack(cands, budget=200).names(), first)


class TestRequired(unittest.TestCase):
    def test_required_is_kept_over_a_higher_priority_optional(self):
        p = pack([c("must", 100, priority=0, required=True),
                  c("want", 100, priority=99)], budget=150)
        self.assertIn("must", p.names())
        self.assertNotIn("want", p.names())

    def test_required_that_does_not_fit_raises(self):
        with self.assertRaises(WontFit):
            pack([c("huge", 1000, required=True)], budget=100)

    def test_required_raises_rather_than_dropping(self):
        # the failure mode this exists to prevent: a prompt missing the thing
        # the caller declared it could not work without, with no error
        try:
            pack([c("small", 10, required=True), c("huge", 5000, required=True)], budget=100)
        except WontFit as e:
            self.assertIn("huge", str(e))
        else:
            self.fail("expected WontFit")

    def test_required_still_respects_the_reserve(self):
        with self.assertRaises(WontFit):
            pack([c("mid", 60, required=True)], budget=100, reserve=60)


class TestEdges(unittest.TestCase):
    def test_empty_candidate_list(self):
        p = pack([], budget=100)
        self.assertEqual(p.decisions, [])
        self.assertEqual(p.used, 0)

    def test_an_item_larger_than_the_whole_budget_says_so(self):
        p = pack([c("whale", 5000)], budget=100)
        self.assertEqual(p.names(), [])
        self.assertIn("on its own", p.excluded[0].reason)

    def test_a_later_small_item_still_fits_after_a_rejection(self):
        # a packer that stops at the first failure would miss "tiny"
        p = pack([c("whale", 5000), c("tiny", 2)], budget=100)
        self.assertIn("tiny", p.names())

    def test_duplicate_names_raise(self):
        with self.assertRaises(ValueError):
            pack([c("same", 10), c("same", 20)], budget=100)

    def test_zero_budget_raises(self):
        with self.assertRaises(ValueError):
            pack([c("a", 1)], budget=0)

    def test_negative_reserve_raises(self):
        with self.assertRaises(ValueError):
            pack([c("a", 1)], budget=100, reserve=-1)

    def test_reserve_swallowing_the_budget_raises(self):
        with self.assertRaises(ValueError):
            pack([c("a", 1)], budget=100, reserve=100)

    def test_empty_text_candidate_is_included_and_costs_nothing(self):
        p = pack([Candidate(name="blank", text="")], budget=10)
        self.assertEqual(p.names(), ["blank"])
        self.assertEqual(p.used, 0)


class TestReadDir(unittest.TestCase):
    def test_reads_its_own_directory(self):
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        cands = read_dir(here)
        names = {c.name for c in cands}
        self.assertIn("roomy.py", names)
        self.assertIn("test_roomy.py", names)

    def test_skips_noise_directories(self):
        self.assertIn(".git", SKIP_DIRS)
        self.assertIn("node_modules", SKIP_DIRS)

    def test_readme_outranks_a_module(self):
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        by = {c.name: c for c in read_dir(here)}
        if "README.md" in by:
            self.assertGreater(by["README.md"].priority, by["roomy.py"].priority)

    def test_names_are_relative_and_slash_separated(self):
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in read_dir(here):
            self.assertFalse(os.path.isabs(cand.name))
            self.assertNotIn("\\", cand.name)


class TestPlanReporting(unittest.TestCase):
    def test_used_equals_the_sum_of_included(self):
        cands = [c("f%02d" % i, i * 3 + 1) for i in range(15)]
        p = pack(cands, budget=120)
        self.assertEqual(p.used, sum(d.tokens for d in p.included))

    def test_headroom_is_what_is_left(self):
        p = pack([c("a", 10)], budget=100, reserve=20)
        self.assertEqual(p.headroom, 100 - 20 - p.used)

    def test_included_and_excluded_partition_the_decisions(self):
        cands = [c("f%02d" % i, i * 5 + 1) for i in range(12)]
        p = pack(cands, budget=90)
        self.assertEqual(len(p.included) + len(p.excluded), len(p.decisions))
        self.assertEqual(set(), {d.name for d in p.included} & {d.name for d in p.excluded})


if __name__ == "__main__":
    unittest.main(verbosity=2)
