"""The body's own workplace policy, and what it is allowed to override.

Every rule here is built explicitly rather than read from
`jobboards.location_rule()` -- that reads this machine's config, and a
test that reads the machine is a test that only runs on one machine
(the 2026-07-30 aihealth-isolation lesson).

The sentences under test are REAL, lifted verbatim from the corpus these
readers were measured against, curly apostrophes and stray markdown
included. A tidied-up paraphrase would pass against parsing that the
actual postings defeat -- which is exactly how the manufactured "Remote"
survived as long as it did.
"""
import unittest

from server import applications, jobboards, workplace


def rule(places=("New York", "NYC", "NY"), remote_ok=True,
         remote_regions=("United States", "US East", "Eastern",
                         "tri-state")):
    return {
        "places": jobboards._rx(list(places)) if places else None,
        "remote_regions": (jobboards._rx(list(remote_regions))
                           if remote_regions else None),
        "remote_ok": remote_ok,
        "exclude": jobboards._rx(REMOTE_EXCLUDE),
        "hints": jobboards._rx(REGION_HINTS),
    }


BOARD = {"company": "TestCo", "ats": "ashby", "slug": "testco"}

REMOTE_EXCLUDE = r"europe|emea|germany|seoul"
REGION_HINTS = r"United States|San Francisco|New York"

# The corpus's single most common policy sentence: 221 of OpenAI's 735
# listed postings carried this shape while the board flagged them remote.
HYBRID_SF = ("This role is based in San Francisco, CA. We use a hybrid "
             "work model of 3 days in the office per week and offer "
             "relocation assistance to new employees.")

ABRIDGE_HYBRID_SF = (
    "This is a hybrid role with a minimum of 3 days weekly in our San "
    "Francisco office. Please only apply if you are able to commit to "
    "this model and/or willing to relocate to San Francisco.")

ASANA_REMOTE_HYBRID = (
    "This role can either be fully remote depending on which US state "
    "you live in, or based in our New York City office with an "
    "office-centric hybrid schedule. If based in-office: The standard "
    "in-office days are Monday, Tuesday, and Thursday. Most Asanas have "
    "the option to work from home on Wednesdays. Working from home on "
    "Fridays depends on the type of work you do and the teams with which "
    "you partner.")

SCALE_HYBRID_SF_NYC = (
    "This is a hybrid role (3 days per week in office) based in our San "
    "Francisco or New York City office to join our team and participate "
    "in the hiring process from beginning to end.")

ABRIDGE_IN_PERSON_WEEKDAYS = (
    "This role is based out of our San Francisco office and is in-person "
    "Monday, Wednesday, and Friday. Please only apply if you are open to "
    "relocation and our hybrid in-person model in San Francisco.")


class TheReader(unittest.TestCase):
    def test_it_says_nothing_when_the_body_says_nothing(self):
        self.assertIsNone(workplace.read(""))
        self.assertIsNone(workplace.read(
            "We are looking for a thoughtful engineer to join the team."))

    def test_a_hybrid_office_binds_and_carries_its_schedule(self):
        wp = workplace.read(HYBRID_SF)
        self.assertTrue(wp["binds"])
        self.assertFalse(wp["remote_ok"])
        self.assertEqual(wp["mode"], "hybrid")
        self.assertEqual(wp["days"], 3)
        self.assertEqual(wp["places"], ["San Francisco, CA"])

    def test_a_schedule_first_hybrid_names_its_binding_office(self):
        # Verbatim Abridge policy: there is no "role is based" trigger,
        # and both "hybrid role" and "3 days weekly" are real corpus
        # forms. The office still has to be read from the body.
        wp = workplace.read(ABRIDGE_HYBRID_SF)
        self.assertTrue(wp["binds"])
        self.assertFalse(wp["remote_ok"])
        self.assertEqual(wp["mode"], "hybrid")
        self.assertEqual(wp["days"], 3)
        self.assertEqual(wp["places"], ["San Francisco"])

    def test_schedule_follow_on_weekdays_are_not_places(self):
        # Verbatim Asana policy. The schedule trigger starts after the
        # remote option and after the real NYC office; generic scanning
        # for a later "based" used to turn Tuesday and Thursday into
        # office names.
        wp = workplace.read(ASANA_REMOTE_HYBRID)
        self.assertEqual(wp["places"], [])

    def test_schedule_first_based_clause_keeps_both_offices(self):
        # Verbatim Scale policy. The words after "office" are unrelated
        # prose and must not cause the second office to be discarded.
        wp = workplace.read(SCALE_HYBRID_SF_NYC)
        self.assertEqual(wp["places"], ["San Francisco", "New York City"])

    def test_residency_clause_stops_before_in_person_weekdays(self):
        # Verbatim Abridge policy. The weekday schedule qualifies the San
        # Francisco office; it does not add Wednesday and Friday as places.
        wp = workplace.read(ABRIDGE_IN_PERSON_WEEKDAYS)
        self.assertEqual(wp["places"], ["San Francisco"])

    def test_location_heading_and_toronto_transition_are_binding(self):
        heading = workplace.read(
            "Location / work model: San Francisco, CA; hybrid, 3 days/week "
            "in-office. Please note this policy.")
        transition = workplace.read(
            "This Toronto-based role is currently remote and is expected to "
            "transition to an in-office arrangement.")
        self.assertEqual(heading["places"], ["San Francisco, CA"])
        self.assertEqual(transition["places"], ["Toronto"])
        self.assertTrue(heading["binds"])
        self.assertTrue(transition["binds"])

    def test_functional_use_of_hybrid_is_not_a_workplace_policy(self):
        self.assertIsNone(workplace.read(
            "This is a unique hybrid role designed for a technically fluent "
            "risk architect. You will own the program end-to-end."))

    def test_the_count_is_read_through_the_words_between_it_and_week(self):
        # "3 days in the office per week" -- a tighter pattern reported no
        # schedule at all on the corpus's most common sentence.
        self.assertEqual(workplace.read(HYBRID_SF)["days"], 3)
        self.assertEqual(workplace.read(
            "**This role is based in San Francisco, CA, and requires "
            "in-person presence 4 days a week.")["days"], 4)
        self.assertEqual(workplace.read(
            "**This role is based out of our New York City office "
            "(5 days per week).")["days"], 5)

    def test_a_curly_apostrophe_does_not_invert_a_refusal(self):
        # The corpus writes "aren't" with U+2019. Missing the contraction
        # let the PERMISSIVE pattern match "considering ... remote" and
        # read a refusal as an offer -- the worst answer available.
        wp = workplace.read(
            "Location & Workplace This role is based in our San Francisco "
            "office and we aren’t considering remote applications at "
            "this time.")
        self.assertTrue(wp["binds"])
        self.assertFalse(wp["remote_ok"])
        self.assertEqual(wp["places"], ["San Francisco"])

    def test_an_open_remote_path_is_not_a_binding_policy(self):
        for sentence in (
            "This role is based in San Francisco or NYC, with a hybrid "
            "schedule of 3 days per week in the office, or can be performed "
            "remotely from anywhere in the U.S.",
            "**This role is either fully remote or based in San Francisco, "
            "CA.",
            "This role is ideally based in San Francisco or New York City, "
            "but we welcome remote candidates.",
            "The role is preferred to be based in San Francisco, Seattle or "
            "New York City but may consider remote work.",
            "## Role Specific Location Policy: - This role is based in San "
            "Francisco office; however, we are open to considering "
            "exceptional candidates for remote work on a case-by-case basis.",
        ):
            wp = workplace.read(sentence)
            self.assertTrue(wp["remote_ok"], sentence[:60])
            self.assertFalse(wp["binds"], sentence[:60])

    def test_determiners_and_office_nouns_are_not_part_of_the_place(self):
        # "in our San Francisco HQ" stacks a preposition on a determiner,
        # so one stripping pass left "our San Francisco" behind.
        self.assertEqual(
            workplace.read("This role is exclusively based in our San "
                           "Francisco HQ. We offer relocation assistance "
                           "to new employee.")["places"],
            ["San Francisco"])

    def test_a_state_or_country_stays_attached_but_a_city_list_splits(self):
        self.assertEqual(
            workplace.read("This role is based in San Francisco, Seattle "
                           "or New York.")["places"],
            ["San Francisco", "Seattle", "New York"])
        self.assertEqual(
            workplace.read("This role is based in one of our European "
                           "offices (Paris, France and London, UK).")["places"],
            ["Paris, France", "London, UK"])
        self.assertEqual(
            workplace.read("This role is based in Tokyo, Japan. We use a "
                           "hybrid work model of 3 days in the office per "
                           "week.")["places"],
            ["Tokyo, Japan"])

    def test_a_schedule_in_parentheses_is_not_a_place(self):
        self.assertEqual(
            workplace.read("**This role is based out of our New York City "
                           "office (5 days per week).")["places"],
            ["New York City"])

    def test_an_arrangement_word_is_never_read_as_an_office(self):
        # "based on-site, five days a week" names no city at all, and
        # reading "on-" and "five days a week" as offices would bind the
        # role to nowhere real.
        wp = workplace.read("This role is based on-site, five days a week "
                            "- remote work not considered")
        self.assertEqual(wp["places"], [])
        self.assertTrue(wp["binds"])
        self.assertEqual(wp["days"], 5)

    def test_prose_that_follows_the_word_based_is_not_an_office(self):
        # Seen on a real OpenAI posting: "...based in San Francisco or an
        # OpenAI self-build data center campus." The second half is prose
        # and would have been shown to the owner as a place name.
        self.assertEqual(
            workplace.read("This role is based in San Francisco or an "
                           "OpenAI self-build data center campus.")["places"],
            ["San Francisco"])

    def test_a_schedule_alone_binds_even_with_no_office_named(self):
        wp = workplace.read("We use a hybrid work model of 3 days in the "
                            "office per week.")
        self.assertTrue(wp["binds"])
        self.assertEqual(wp["places"], [])   # the board's locations decide

    def test_markdown_and_headings_do_not_hide_the_policy(self):
        self.assertEqual(
            workplace.read("## Workplace & Location **This role is based "
                           "in San Francisco, CA.**")["places"],
            ["San Francisco, CA"])


class WhatABodyMayOverride(unittest.TestCase):
    def test_a_policy_that_does_not_bind_allows_everything(self):
        self.assertTrue(workplace.allows(None, rule()["places"], ["Remote"]))
        wp = workplace.read("**This role is either fully remote or based "
                            "in San Francisco, CA.")
        self.assertTrue(workplace.allows(wp, rule()["places"], ["Remote"]))

    def test_a_body_naming_the_owners_own_city_allows(self):
        wp = workplace.read("This role is based in New York City. We use a "
                            "hybrid work model of 3 days in the office per "
                            "week.")
        self.assertTrue(workplace.allows(wp, rule()["places"],
                                         ["New York City"]))

    def test_a_posting_that_names_no_city_at_all_refuses(self):
        # The headline case: eligibility rested entirely on a remote tag
        # the body contradicts, and nothing published is a city.
        wp = workplace.read(HYBRID_SF)
        self.assertFalse(workplace.allows(wp, rule()["places"],
                                          ["US - Remote"]))

    def test_a_body_narrowing_a_published_list_refuses(self):
        # The posting lists three cities including one the owner works in;
        # the body says only one of them is real. That is narrowing.
        wp = workplace.read("This role is exclusively based in our San "
                            "Francisco HQ.")
        self.assertFalse(workplace.allows(
            wp, rule()["places"],
            ["San Francisco", "New York City", "Seattle"]))

    def test_an_office_the_posting_never_named_does_not_relocate_a_role(self):
        # Hebbia's "AI Strategist, Corporate Law": listed in NYC, body says
        # "based in our SoHo office". SoHo matches no New York rule, and
        # vetoing on it would hide a real New York job.
        wp = workplace.read("This role is based in our SoHo office and "
                            "follows a hybrid schedule of 5 days a week.")
        self.assertEqual(wp["places"], ["SoHo"])
        self.assertTrue(workplace.allows(wp, rule()["places"], ["NYC"]))

    def test_place_matching_ignores_tokens_that_distinguish_nothing(self):
        wp = workplace.read("This role is based in New Orleans.")
        # "New" alone must not make New Orleans corroborate New York.
        self.assertTrue(workplace.allows(wp, rule()["places"], ["New York"]))
        # but a real match still corroborates, spelling differences included
        wp2 = workplace.read("This role is based in Washington, D.C.")
        self.assertFalse(workplace.allows(wp2, rule()["places"],
                                          ["Washington, DC", "New York"]))

    def test_a_schedule_with_no_office_binds_to_the_posted_locations(self):
        # "hybrid, 3 days/week" names no city, but you cannot be in an
        # office three days a week from another one -- so the posting's
        # own on-site locations are what it binds to, and the remote tag
        # is exactly what it contradicts.
        wp = workplace.read("We use a hybrid work model of 3 days in the "
                            "office per week.")
        self.assertEqual(wp["places"], [])
        self.assertFalse(workplace.allows(
            wp, rule()["places"], ["San Francisco", "Seattle", "Remote"]))
        self.assertTrue(workplace.allows(
            wp, rule()["places"], ["Toronto", "New York", "Remote"]))

    def test_a_schedule_without_a_confirmed_configured_base_refuses(self):
        wp = workplace.read("We use a hybrid work model of 3 days in the "
                            "office per week.")
        self.assertFalse(workplace.allows(wp, rule()["places"], ["Remote"]))

    def test_an_unmatched_region_limited_remote_role_refuses(self):
        wp = workplace.read(
            "This role is remote but the successful candidate should be "
            "based in Germany or surrounding area. About the role")
        self.assertTrue(wp["remote_limited"])
        self.assertTrue(wp["remote_ok"])
        configured = rule()
        self.assertFalse(workplace.allows(
            wp, configured["places"], ["Remote"],
            configured["remote_regions"]))

    def test_explicitly_configured_remote_territories_are_allowed(self):
        east = workplace.read(
            "This role is remote (must be located in US East, Central, or "
            "West timezone). Compensation and benefits")
        national = workplace.read(
            "Candidates must be based in the United States; remote work is "
            "also possible. About the role")
        configured = rule()
        self.assertTrue(workplace.allows(
            east, configured["places"], ["Remote"],
            configured["remote_regions"]))
        self.assertTrue(workplace.allows(
            national, configured["places"], ["Remote"],
            configured["remote_regions"]))

    def test_a_configured_tri_state_territory_is_allowed(self):
        wp = workplace.read(
            "This role is remote (must be based in the tri-state area). "
            "Compensation and benefits")
        configured = rule()
        self.assertTrue(workplace.allows(
            wp, configured["places"], ["New York, NY"],
            configured["remote_regions"]))

    def test_remote_territories_are_configuration_not_product_policy(self):
        wp = workplace.read(
            "This role is remote but the successful candidate should be "
            "based in Germany. About the role")
        office = jobboards._rx(["Berlin"])
        germany = jobboards._rx(["Germany"])
        canada = jobboards._rx(["Canada"])
        self.assertTrue(workplace.allows(
            wp, office, ["Remote"], germany))
        self.assertFalse(workplace.allows(
            wp, office, ["Remote"], canada))

    def test_binding_office_uses_only_configured_places(self):
        wp = workplace.read(
            "This role is based in San Francisco. We use a hybrid work "
            "model of 3 days in the office per week.")
        self.assertTrue(workplace.allows(
            wp, jobboards._rx(["San Francisco"]), ["Remote"]))
        self.assertFalse(workplace.allows(
            wp, jobboards._rx(["New York"]), ["Remote"]))

    def test_anthropic_office_presence_needs_an_explicit_nyc_base(self):
        wp = workplace.read(
            "Location-based hybrid policy: Currently, we expect all staff "
            "to be in one of our offices at least 25% of the time. However, "
            "some roles may require more time in our offices.")
        self.assertFalse(workplace.allows(
            wp, rule()["places"], ["Remote-Friendly, United States"]))
        self.assertTrue(workplace.allows(
            wp, rule()["places"], ["New York, NY", "Remote-Friendly"]))

    def test_abbreviated_sf_and_ny_offices_include_new_york(self):
        wp = workplace.read(
            "Must be willing to work from our SF or NY office 3x per week.")
        self.assertEqual(wp["places"], ["SF", "NY"])
        self.assertTrue(workplace.allows(
            wp, rule()["places"], ["SF Office", "NYC Office", "Remote"]))

    def test_an_unconfigured_rule_never_refuses(self):
        wp = workplace.read(HYBRID_SF)
        self.assertTrue(workplace.allows(wp, None, ["San Francisco"]))


class TheBoardFlagIsNotTheEmployersWord(unittest.TestCase):
    def test_a_remote_flag_is_dropped_when_the_body_contradicts_it(self):
        rec = jobboards._norm(BOARD, uid="u1", title="SWE",
                              locations=["San Francisco"], jd=HYBRID_SF,
                              remote_flag=True)
        self.assertEqual(rec["locations"], ["San Francisco"])
        self.assertEqual(rec["remote"], "")
        self.assertTrue(rec["workplace"]["binds"])

    def test_a_remote_flag_is_honoured_when_the_body_is_silent(self):
        rec = jobboards._norm(BOARD, uid="u2", title="SWE",
                              locations=["San Francisco"],
                              jd="We are hiring an engineer.",
                              remote_flag=True)
        self.assertEqual(rec["locations"], ["San Francisco", "Remote"])
        self.assertEqual(rec["remote"], "remote")

    def test_a_remote_flag_is_honoured_when_the_body_allows_remote(self):
        rec = jobboards._norm(
            BOARD, uid="u3", title="SWE", locations=["San Francisco"],
            jd="**This role is either fully remote or based in San "
               "Francisco, CA.", remote_flag=True)
        self.assertIn("Remote", rec["locations"])

    def test_a_location_the_board_published_is_never_rewritten(self):
        # "US - Remote" is the employer's own word. The disagreement is
        # surfaced through `workplace`, never resolved by editing them.
        rec = jobboards._norm(BOARD, uid="u4", title="SWE",
                              locations=["US - Remote"], jd=HYBRID_SF,
                              remote_flag=True)
        self.assertEqual(rec["locations"], ["US - Remote"])
        self.assertTrue(rec["workplace"]["binds"])

    def test_a_region_limited_remote_role_keeps_the_remote_flag(self):
        rec = jobboards._norm(
            BOARD, uid="u5", title="SWE", locations=["Germany"],
            jd="This role is remote but the successful candidate should be "
               "based in Germany.", remote_flag=True)
        self.assertIn("Remote", rec["locations"])
        self.assertEqual(rec["remote"], "remote")


class Eligibility(unittest.TestCase):
    def test_the_body_refuses_a_role_the_location_field_called_remote(self):
        rec = {"locations": ["US - Remote"], "workplace": workplace.read(HYBRID_SF)}
        self.assertTrue(jobboards.eligible_location(
            {"locations": ["US - Remote"]}, rule()))       # before
        self.assertFalse(jobboards.eligible_location(rec, rule()))

    def test_a_remote_tag_cannot_override_abridge_hybrid_sf(self):
        rec = {"locations": ["Remote"],
               "workplace": workplace.read(ABRIDGE_HYBRID_SF)}
        self.assertFalse(jobboards.eligible_location(rec, rule()))

    def test_asana_state_remote_is_not_narrowed_by_weekdays(self):
        rec = {"locations": ["US IL Remote"],
               "workplace": workplace.read(ASANA_REMOTE_HYBRID)}
        self.assertTrue(jobboards.eligible_location(rec, rule()))

    def test_scale_hybrid_includes_its_new_york_office(self):
        rec = {"locations": ["San Francisco", "New York City"],
               "workplace": workplace.read(SCALE_HYBRID_SF_NYC)}
        self.assertTrue(jobboards.eligible_location(rec, rule()))

    def test_the_reading_only_ever_narrows(self):
        # A role the location rule already refuses cannot be rescued by a
        # body reading, whatever it says.
        rec = {"locations": ["Tokyo, Japan"],
               "workplace": workplace.read(
                   "**This role is either fully remote or based in Tokyo.")}
        self.assertFalse(jobboards.eligible_location(rec, rule()))

    def test_an_unconfigured_install_is_still_unfiltered(self):
        rec = {"locations": ["San Francisco"], "workplace": workplace.read(HYBRID_SF)}
        self.assertTrue(jobboards.eligible_location(
            rec, rule(places=None, remote_ok=True, remote_regions=None)))


class TheFacetsAndTheStamp(unittest.TestCase):
    def test_a_bound_role_stops_answering_the_remote_filter(self):
        wp = workplace.read(HYBRID_SF)
        self.assertEqual(
            applications.places_for(["US - Remote"], wp),
            ["San Francisco"])          # the body's office, not Remote

    def test_facets_are_untouched_when_the_body_does_not_bind(self):
        self.assertEqual(
            applications.places_for(["US - Remote", "San Francisco"], None),
            ["Remote", "San Francisco"])

    def test_board_bullets_split_into_real_place_facets(self):
        self.assertEqual(
            applications.places_for(
                ["San Francisco, CA • New York, NY • United States"], None),
            ["San Francisco", "New York", "United States"])

    def test_region_limited_remote_keeps_its_remote_facet(self):
        wp = workplace.read(
            "This role is remote but the successful candidate should be "
            "based in Germany.")
        self.assertEqual(applications.places_for(["Germany", "Remote"], wp),
                         ["Germany", "Remote"])

    def test_a_stale_stamp_is_vetoed_by_the_body(self):
        src = {"slug": "x", "company": "X"}
        row = applications._norm(
            {"title": "T", "eligible": True, "locations": ["US - Remote"],
             "jd": HYBRID_SF,
             "url": "https://jobs.ashbyhq.com/x/aaaa-bbbb"},
            src, {}, rule())
        self.assertIs(row["eligible"], False)
        self.assertEqual(row["workplace_label"],
                         "San Francisco, CA - hybrid, 3 days/week")

    def test_the_veto_cannot_manufacture_eligibility(self):
        # A stamped False stays False even where the body is permissive.
        src = {"slug": "x", "company": "X"}
        row = applications._norm(
            {"title": "T", "eligible": False, "locations": ["New York, NY"],
             "jd": "**This role is either fully remote or based in NYC.",
             "url": "https://jobs.ashbyhq.com/x/cccc-dddd"},
            src, {}, rule())
        self.assertIs(row["eligible"], False)


if __name__ == "__main__":
    unittest.main()
