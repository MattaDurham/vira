"""The propose_idea blocking gate needs an ABSOLUTE similarity condition.

`_cos_rel` normalises against the corpus's own spread, which is right for
ranking and wrong for refusing: on a backlog whose items are all "a feature
idea for one person's own systems", the nearest neighbour of ANY new idea
sits at or near p99, rel saturates at 1.0, and the fused score clears
DUP_FLOOR on the vector term alone. Measured on the live 186-idea backlog
2026-08-26: eight genuinely new proposals refused at rel 0.996-1.000 with
raw cosine 0.771-0.814, while the backlog's own restatement family (one
test_jobrescore bug filed six times) runs 0.860-0.935.

The fixture reproduces that geometry deterministically - no Ollama, no
live store - by CONSTRUCTING each candidate at an exact cosine to one
corpus item. Every candidate is built in a dimension the corpus does not
span, so its intended neighbour is provably its nearest (see _candidate).
"""
import unittest
from unittest import mock

import numpy as np

from server import ideatags


# The corpus spans dims 0..62; dim 63 is reserved as the off-corpus axis.
# High dimension is deliberate: near-orthogonal noise concentrates the
# pairwise band the way a real single-subject backlog does. These constants
# were tuned against the live 2026-08-26 distribution (min 0.361 / p50 0.622
# / p90 0.700 / p99 0.771) and land at min 0.489 / p50 0.621 / p90 0.686 /
# p99 0.739 - see test_the_fixture_reproduces_the_narrow_band.
DIM = 64
OFF_AXIS = 63
NOISE_DIMS = 62
BASE_WEIGHT = 1.28


def _unit(v):
    return v / (np.linalg.norm(v) + 1e-9)


def _corpus_vectors(n, seed=7):
    """n unit vectors sharing a strong component, so pairwise cosine lands
    in a narrow band the way a single-subject backlog really does."""
    rng = np.random.default_rng(seed)
    base = np.zeros(DIM)
    base[0] = 1.0
    out = []
    for _ in range(n):
        noise = np.zeros(DIM)
        noise[1:1 + NOISE_DIMS] = rng.normal(size=NOISE_DIMS)
        out.append(_unit(BASE_WEIGHT * base + _unit(noise)))
    return np.stack(out)


def _candidate(target_vec, cos):
    """A unit vector whose cosine to `target_vec` is exactly `cos`.

    The orthogonal part rides OFF_AXIS, a dimension no corpus vector uses,
    so for any other corpus item j:
        cos(cand, v_j) = cos * (target . v_j) <= cos
    i.e. the intended neighbour is the nearest by construction. That is
    what makes "the gate refused this candidate against THIS item" a fact
    about the threshold rather than about which row happened to win.
    """
    off = np.zeros(DIM)
    off[OFF_AXIS] = 1.0
    return _unit(cos * target_vec + np.sqrt(max(0.0, 1 - cos * cos)) * off)


class DupGateFixture(unittest.TestCase):
    """One tmp corpus; nothing here reads the machine or the live store."""

    N = 40

    def setUp(self):
        self.vecs = _corpus_vectors(self.N)
        self.items = [
            {"id": f"idea_{i:04d}", "text": f"corpus idea number {i}",
             "project": "Vira", "status": "open"}
            for i in range(self.N)
        ]
        entries = {}
        for it, v in zip(self.items, self.vecs):
            entries[it["id"]] = {
                "vec": ideatags._pack(v),
                "vhash": ideatags._hash(ideatags._embed_text(it)),
            }
        self.store = {"entries": entries}
        ideatags._base_cache.clear()
        self.addCleanup(ideatags._base_cache.clear)
        p = mock.patch.object(ideatags, "_read", return_value=self.store)
        p.start()
        self.addCleanup(p.stop)

    def check(self, cos_to_neighbour, neighbour=0, text=None, **kw):
        """Score a candidate placed at an exact cosine from one item."""
        vec = _candidate(self.vecs[neighbour], cos_to_neighbour)
        with mock.patch.object(ideatags, "_candidate_vector",
                               return_value=vec):
            return ideatags.check_candidate(
                text or "a genuinely different proposal about something else",
                project="Vira", items=self.items, **kw)

    # ---- the fixture itself has to be sound before anything it proves ----

    def test_the_fixture_reproduces_the_narrow_band(self):
        off = (self.vecs @ self.vecs.T)[~np.eye(self.N, dtype=bool)]
        p50 = float(np.percentile(off, 50))
        p99 = float(np.percentile(off, 99))
        self.assertGreater(p50, 0.55)
        self.assertLess(p50, 0.70)
        # The whole defect depends on a candidate being ABLE to sit above
        # the corpus's p99 while still being far from anything. If the
        # fixture band were wide this file would prove nothing, so p99 must
        # sit below even the LOWEST wrongly-refused cosine (0.7707).
        self.assertLess(p99, 0.7707)
        # And no ordinary corpus pair may reach the absolute floor, or the
        # refusal cases below could pass on a neighbour nobody intended.
        self.assertLess(off.max(), ideatags.DUP_COS_ABS)

    def test_the_intended_neighbour_is_really_the_nearest(self):
        vec = _candidate(self.vecs[3], 0.81)
        sims = self.vecs @ vec
        self.assertEqual(int(np.argmax(sims)), 3)
        self.assertAlmostEqual(float(sims[3]), 0.81, places=5)

    # ---- the defect ----

    def test_a_distant_candidate_at_corpus_p99_is_not_refused(self):
        """The eight-refusals case: rel saturates, cosine says otherwise."""
        res = self.check(0.81)
        self.assertEqual(res["basis"], "vector")
        self.assertEqual(res["matches"], [])

    def test_that_candidate_really_did_saturate_the_relative_score(self):
        """Without this the test above could pass for the wrong reason -
        a candidate nothing scored highly proves nothing about the floor."""
        res = self.check(0.81, cos_floor=0.0)
        self.assertTrue(res["matches"], "fixture never reached DUP_FLOOR")
        top = res["matches"][0]
        self.assertGreaterEqual(top["score"], ideatags.DUP_FLOOR)
        self.assertGreaterEqual(top["closeness"], 0.99)
        self.assertLess(top["cos"], ideatags.DUP_COS_ABS)

    def test_a_real_restatement_is_still_refused(self):
        """0.90 is inside the live backlog's restatement family (0.860 to
        0.935). The gate must keep doing the job it exists for."""
        res = self.check(0.90)
        self.assertTrue(res["matches"])
        self.assertGreaterEqual(res["matches"][0]["cos"],
                                ideatags.DUP_COS_ABS)

    def test_the_floor_sits_between_the_two_measured_populations(self):
        """Pins the number against what was measured rather than taste:
        0.814 was the highest wrongly-refused candidate, 0.860 the lowest
        genuine restatement."""
        self.assertGreater(ideatags.DUP_COS_ABS, 0.814)
        self.assertLessEqual(ideatags.DUP_COS_ABS, 0.860)

    def test_every_wrongly_refused_cosine_now_passes(self):
        """The eight real cosines from 2026-08-26, as a labelled set."""
        measured = {
            "waiting-lane": 0.7737, "subs-resolution": 0.8140,
            "claim-gate": 0.8016, "lessons-harvest": 0.7788,
            "coverage-ledger": 0.7915, "runway": 0.7707,
            "backup-restore": 0.7776, "murmur-wer": 0.7890,
        }
        for label, cos in measured.items():
            with self.subTest(label):
                self.assertEqual(self.check(cos)["matches"], [],
                                 f"{label} refused again at cos {cos}")

    def test_every_genuine_restatement_cosine_still_refuses(self):
        """The test_jobrescore family's real pairwise cosines."""
        for cos in (0.8602, 0.8796, 0.8833, 0.9044, 0.9066, 0.9279, 0.9354):
            with self.subTest(cos=cos):
                self.assertTrue(self.check(cos)["matches"],
                                f"restatement at cos {cos} slipped through")

    # ---- the degrade, and the scope ----

    def test_no_vector_falls_back_to_wording_rather_than_refusing_all(self):
        """Ollama down: there is no cosine to test, so the condition is
        SKIPPED, not failed - the score is then tag and token overlap,
        which already demands real shared wording."""
        with mock.patch.object(ideatags, "_candidate_vector",
                               return_value=None):
            same = ideatags.check_candidate(
                self.items[0]["text"], project="Vira", items=self.items)
            other = ideatags.check_candidate(
                "an unrelated proposal concerning entirely other matters",
                project="Vira", items=self.items)
        self.assertEqual(same["basis"], "text")
        self.assertTrue(same["matches"], "verbatim text stopped matching")
        self.assertEqual(other["matches"], [])

    def test_the_advisory_surfaces_keep_the_permissive_score(self):
        """Scope pin. The nudge and the Similar panel are dismissible, so
        they are deliberately NOT gated on the absolute cosine - only the
        path that BLOCKS is. If duplicates() ever starts filtering on it,
        this fails and the decision gets re-made on purpose."""
        pairs = ideatags.duplicates(items=self.items, limit=200)
        self.assertTrue(
            any(p["score"] >= ideatags.DUP_FLOOR for p in pairs),
            "advisory duplicates() went silent - it should not be gated")


if __name__ == "__main__":
    unittest.main()
