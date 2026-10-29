"""server/names.py: the one answer to "could these two names be one
person?" that the merge, the fold and the import check share."""

import unittest

from server import names


class NamesTest(unittest.TestCase):
    def test_split_drops_suffixes_and_parentheticals(self):
        self.assertEqual(names.split("Pat Q. Example"), ("Pat Q.", "Example"))
        self.assertEqual(names.split("Pat Example, CFP"), ("Pat", "Example"))
        self.assertEqual(names.split("Pat Example Jr."), ("Pat", "Example"))
        self.assertEqual(names.split("J. Example (first name unconfirmed)"), ("J.", "Example"))
        self.assertEqual(names.split("Example"), ("Example", ""))
        self.assertEqual(names.split(""), ("", ""))
        self.assertEqual(names.split("Twyla Eilertsen Ruby"), ("Twyla Eilertsen", "Ruby"))

    def test_nicknames_are_one_first_name(self):
        self.assertTrue(names.same_first("Matt", "Matthew"))
        self.assertTrue(names.same_first("Bob", "Rob"))
        self.assertTrue(names.same_first("Pat Q.", "pat"))
        self.assertFalse(names.same_first("Pat", "Sam"))
        self.assertFalse(names.same_first("J.", "J."))        # an initial is no match
        self.assertFalse(names.same_first("", "Pat"))
        self.assertEqual(names.nick_set("J."), set())
        self.assertIn("liz", names.nick_set("Elizabeth"))

    def test_compatible_needs_the_surname_too(self):
        self.assertEqual(names.why("Matt Example", "Matthew Example"), "nickname")
        self.assertEqual(names.why("Pat Example", "Pat Example"), "same name")
        self.assertEqual(names.why("Pat Example", "Pat Other"), "")
        self.assertEqual(names.why("Pat", "Pat Example"), "")
        self.assertEqual(names.why("Twyla Eilertsen Ruby", "Twyla Ruby"), "same name")
        self.assertFalse(names.compatible("Acme Client Services (role account)",
                                          "Acme Client Services SF (role account)"))
        self.assertTrue(names.compatible("Sean M. Example, CFP", "Sean Example"))

    def test_agree_is_the_shared_handle_gate(self):
        self.assertTrue(names.agree("", "Pat Example"))              # an unnamed card
        self.assertTrue(names.agree("Pat Example", "Patricia Example"))
        self.assertTrue(names.agree("Ariana Example", "Arianna"))     # a spelling slip
        # two surnames on one handle is never folded on a shared handle
        # alone, even with one first name: a fold deletes a card
        self.assertFalse(names.agree("Pat Example", "Pat Other"))
        self.assertFalse(names.agree("Lee Fifth", "Kim Sixth"))       # colleagues on a line
        self.assertFalse(names.agree("Jeany", "Jennifer Example"))    # no bridge
        self.assertFalse(names.agree("Jeany", "Lee Fifth"))
        self.assertFalse(names.agree("Ariana Example", "Arianna Other"))
        self.assertTrue(names.similar_first("Ariana", "Arianna"))
        self.assertFalse(names.similar_first("Al", "Ali"))
        self.assertFalse(names.similar_first("Pat", "Sam"))
