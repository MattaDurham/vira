"""The reply channel.

Rooted at ONE tmp fixture, because this module reads and writes four stores
outside itself (the notify ledger, the notify config, and its own state and
log) and reaches three subsystems (sessions, calinvite, the send path).
`test_the_fixture_isolates_every_store_the_router_writes` is the guard: a
source added later that reads the real checkout fails it on sight.

Nothing here may send an iMessage or answer an invitation. Every outbound
path is a recorder, and the safety contract — an outward action is HELD and
confirmed, never taken on the first message — has a test of its own.
"""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import calinvite, inbound, notify

HANDLE = "+12125550100"   # NANP 555-01xx fiction block


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for mod, attr, name in ((notify, "LOG", "notify-log.json"),
                                (notify, "CONFIG", "config.json"),
                                (inbound, "STATE", "inbound-state.json"),
                                (inbound, "LOG", "inbound-log.json")):
            p = mock.patch.object(mod, attr, self.tmp / name)
            p.start()
            self.addCleanup(p.stop)
        notify.CONFIG.write_text(json.dumps(
            {"notify_enabled": True, "notify_handle": HANDLE}), encoding="utf-8")

        self.sent = []
        p = mock.patch.object(
            notify, "channel_send",
            lambda text, kind="reply", ref=None: (
                self.sent.append(text) or True))
        p.start(); self.addCleanup(p.stop)

        # Nothing in this suite may reach a real invitation.
        self.rsvps = []

        def fake_rsvp(account, rowid, answer, *, dry_run=False):
            self.rsvps.append({"account": account, "rowid": rowid,
                               "answer": answer, "dry_run": dry_run})
            return {"ok": True, "via": "respond-link", "dry_run": dry_run,
                    "event": "Sunday planning", "detail": "did the thing"}
        p = mock.patch.object(calinvite, "rsvp", fake_rsvp)
        p.start(); self.addCleanup(p.stop)

        p = mock.patch.object(inbound, "enabled", lambda: True)
        p.start(); self.addCleanup(p.stop)
        # No supervisor and no parked session unless a case says so.
        for name in ("_pending_card", "_bound_session"):
            p = mock.patch.object(inbound, name, lambda *a: None)
            p.start(); self.addCleanup(p.stop)
        self.dispatched = []
        p = mock.patch.object(
            inbound, "_dispatch",
            lambda text: (self.dispatched.append(text)
                          or {"route": "dispatch", "ok": True}))
        p.start(); self.addCleanup(p.stop)

    def invite_ref(self, subject="Invitation: Sunday planning @ Sun 3pm",
                   when=None):
        at = (when or datetime.now()).isoformat(timespec="seconds")
        notify.LOG.write_text(json.dumps({"sent": [{
            "at": at, "ok": True, "channel": "email", "person_id": "p_1",
            "person_name": "A Person", "text": "Vira: A Person emailed",
            "ref": {"kind": "email", "account": "owner@example.com",
                    "rowid": "mail-owner@example.com-1",
                    "subject": subject, "person_name": "A Person"},
        }]}), encoding="utf-8")


class Isolation(Base):
    def test_the_fixture_isolates_every_store_the_router_writes(self):
        real = Path(__file__).resolve().parent.parent / "data"
        before = {p.name: p.stat().st_mtime_ns
                  for p in real.glob("*.json") if p.is_file()}
        self.invite_ref()
        inbound.route("yes")
        inbound.route("something else entirely")
        after = {p.name: p.stat().st_mtime_ns
                 for p in real.glob("*.json") if p.is_file()}
        self.assertEqual(before, after, "the router wrote the real data dir")
        self.assertTrue(inbound.STATE.exists())


class EchoFilter(Base):
    def test_viras_own_notifications_are_never_read_as_instructions(self):
        self.assertTrue(inbound.is_ours("Vira: 2 new jobs — Anthropic"))

    def test_a_machine_sender_without_the_prefix_is_still_not_the_owner(self):
        # 15 of these sit in the real thread. Read as the owner talking,
        # each morning would dispatch a session.
        self.assertTrue(inbound.is_ours("Morning picker ready — 8 videos"))

    def test_an_exact_recent_send_is_an_echo_even_without_the_prefix(self):
        notify.LOG.write_text(json.dumps({"sent": [{
            "at": datetime.now().isoformat(timespec="seconds"),
            "ok": True, "text": "an unprefixed thing Vira said"}]}), encoding="utf-8")
        self.assertTrue(inbound.is_ours("an unprefixed thing Vira said"))

    def test_an_old_send_is_no_longer_an_echo(self):
        old = (datetime.now() - timedelta(hours=6)).isoformat(
            timespec="seconds")
        notify.LOG.write_text(json.dumps({"sent": [
            {"at": old, "ok": True, "text": "yesterday's words"}]}), encoding="utf-8")
        self.assertFalse(inbound.is_ours("yesterday's words"))

    def test_the_owners_own_message_passes_through(self):
        self.assertFalse(inbound.is_ours("Reply yes"))
        self.assertFalse(inbound.is_ours("what's on tomorrow"))

    def test_an_empty_message_is_never_routed(self):
        self.assertTrue(inbound.is_ours("   "))


class OutwardActionsAreHeld(Base):
    def test_an_rsvp_is_confirmed_before_it_is_sent(self):
        # THE safety contract. A texted "yes" must never answer an
        # invitation on its own — it may only ask.
        self.invite_ref()
        disp = inbound.route("yes")
        self.assertEqual(disp["route"], "held")
        self.assertEqual([r["dry_run"] for r in self.rsvps], [True],
                         "the ladder was walked for real, not planned")
        self.assertTrue(any("Reply ok" in s for s in self.sent))

    def test_the_confirmation_names_what_it_would_answer(self):
        # A mis-bound reply has to be visible BEFORE it costs anything.
        self.invite_ref()
        inbound.route("yes")
        self.assertTrue(any("Sunday planning" in s for s in self.sent),
                        self.sent)

    def test_confirming_performs_it(self):
        self.invite_ref()
        inbound.route("yes")
        self.rsvps.clear()
        disp = inbound.route("ok")
        self.assertEqual(disp["route"], "rsvp")
        self.assertEqual(len(self.rsvps), 1)
        self.assertFalse(self.rsvps[0]["dry_run"])
        self.assertEqual(self.rsvps[0]["answer"], "yes")

    def test_declining_the_confirmation_sends_nothing(self):
        self.invite_ref()
        inbound.route("yes")
        self.rsvps.clear()
        disp = inbound.route("no")
        self.assertEqual(disp["route"], "hold-cancelled")
        self.assertEqual(self.rsvps, [])

    def test_cancel_words_drop_the_hold(self):
        self.invite_ref()
        inbound.route("yes")
        self.rsvps.clear()
        self.assertEqual(inbound.route("nevermind")["route"],
                         "hold-cancelled")
        self.assertEqual(self.rsvps, [])

    def test_an_expired_hold_cannot_be_fired_by_a_later_yes(self):
        # Tomorrow's "yes" must not land on today's held action.
        self.invite_ref()
        inbound.route("yes")

        def fn(store):
            store["held"]["expires"] = (
                datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            return store
        inbound._save(fn)
        self.rsvps.clear()
        notify.LOG.write_text(json.dumps({"sent": []}), encoding="utf-8")   # no invite to bind
        inbound.route("ok")
        self.assertEqual(self.rsvps, [], "an expired hold still fired")

    def test_a_new_instruction_supersedes_a_hold_rather_than_arming_it(self):
        self.invite_ref()
        inbound.route("yes")
        self.rsvps.clear()
        inbound.route("what's on tomorrow")
        self.assertIsNone(inbound._state()["held"])
        self.assertEqual(self.rsvps, [])
        self.assertEqual(self.dispatched, ["what's on tomorrow"])


class Binding(Base):
    def test_an_affirmation_with_no_invitation_is_not_forced_into_an_rsvp(self):
        # Nothing to bind to -> it becomes a session, not the nearest
        # known action.
        notify.LOG.write_text(json.dumps({"sent": []}), encoding="utf-8")
        disp = inbound.route("yes")
        self.assertEqual(disp["route"], "dispatch")
        self.assertEqual(self.rsvps, [])

    def test_a_non_invitation_email_ping_does_not_arm_an_rsvp(self):
        self.invite_ref(subject="Re: the quarterly numbers")
        disp = inbound.route("yes")
        self.assertEqual(disp["route"], "dispatch")
        self.assertEqual(self.rsvps, [])

    def test_a_stale_ping_is_out_of_binding_range(self):
        self.invite_ref(when=datetime.now() - timedelta(hours=25))
        disp = inbound.route("yes")
        self.assertEqual(disp["route"], "dispatch")


class Ladder(Base):
    def test_a_waiting_decision_card_outranks_everything_else(self):
        answered = []
        card = {"job_id": "j1", "card": {"req_id": "r1", "kind": "ask",
                                         "question": "which one?"}}
        fake = mock.Mock()
        fake.answer.side_effect = lambda *a: answered.append(a)
        with mock.patch.object(inbound, "_pending_card", lambda: card), \
             mock.patch("server.session.sessions", fake):
            self.invite_ref()
            disp = inbound.route("yes")
        self.assertEqual(disp["route"], "card")
        self.assertEqual(answered, [("j1", "r1", "yes")])
        self.assertEqual(self.rsvps, [], "an RSVP fired past a waiting card")

    def test_a_parked_session_takes_the_message_when_no_card_waits(self):
        fake = mock.Mock()
        fake.say.return_value = {"job": "j2"}
        with mock.patch.object(inbound, "_bound_session", lambda: "j2"), \
             mock.patch("server.session.sessions", fake):
            disp = inbound.route("keep going then")
        self.assertEqual(disp["route"], "steer")
        fake.say.assert_called_once_with("j2", "keep going then")

    def test_anything_unrecognised_becomes_a_session(self):
        disp = inbound.route("what did they say last week")
        self.assertEqual(disp["route"], "dispatch")
        self.assertEqual(self.dispatched, ["what did they say last week"])


class Consume(Base):
    def _item(self, text, handle=HANDLE, **kw):
        d = {"channel": "imessage", "handle": handle, "text": text,
             "group": False}
        d.update(kw)
        return d

    def test_only_the_notify_thread_is_read_as_a_command_line(self):
        routed = []
        with mock.patch.object(inbound, "route",
                               lambda t, i=None: routed.append(t)):
            inbound.consume([
                self._item("do the thing"),
                self._item("not for you", handle="+12125550101"),
                self._item("group chatter", group=True),
                {"channel": "email", "handle": HANDLE, "text": "an email"},
            ])
        self.assertEqual(routed, ["do the thing"])

    def test_viras_own_echo_is_dropped_before_routing(self):
        routed = []
        with mock.patch.object(inbound, "route",
                               lambda t, i=None: routed.append(t)):
            inbound.consume([self._item("Vira: 3 new jobs"),
                             self._item("Morning picker ready — 8 videos")])
        self.assertEqual(routed, [])

    def test_one_bad_reply_never_stops_the_feed(self):
        def boom(text, item=None):
            raise RuntimeError("nope")
        with mock.patch.object(inbound, "route", boom):
            inbound.consume([self._item("anything")])   # must not raise
        self.assertTrue(any(r["route"] == "error" for r in inbound.recent()))


class Dormancy(Base):
    """The real predicate, not the Base stub — captured at import time
    because Base patches the name for every other case in the file."""

    REAL = staticmethod(inbound.enabled)

    def test_it_is_live_when_a_handle_is_configured(self):
        with mock.patch.dict("os.environ", {}, clear=False) as _:
            import os
            os.environ.pop("VIRA_PASSIVE", None)
            self.assertTrue(self.REAL())

    def test_it_is_dormant_without_a_configured_handle(self):
        notify.CONFIG.write_text(json.dumps(
            {"notify_enabled": True, "notify_handle": ""}), encoding="utf-8")
        self.assertFalse(self.REAL())

    def test_it_is_dormant_when_the_owner_turns_it_off(self):
        with mock.patch.object(inbound, "_cfg",
                               lambda key, default: False):
            self.assertFalse(self.REAL())

    def test_a_passive_instance_never_reads_the_thread(self):
        with mock.patch.dict("os.environ", {"VIRA_PASSIVE": "1"}):
            self.assertFalse(self.REAL())

    def test_a_dormant_channel_routes_nothing(self):
        routed = []
        with mock.patch.dict("os.environ", {"VIRA_PASSIVE": "1"}), \
             mock.patch.object(inbound, "enabled", self.REAL), \
             mock.patch.object(inbound, "route",
                               lambda t, i=None: routed.append(t)):
            inbound.consume([{"channel": "imessage", "handle": HANDLE,
                              "text": "do it", "group": False}])
        self.assertEqual(routed, [])


if __name__ == "__main__":
    unittest.main()
