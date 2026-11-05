import contextlib
import http.client
import io
import os
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

import kit_send_check as check

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
OWNER_EMAIL = "owner@example.com"
OWNER_ID = 7
TAG_ID = 42
BROADCAST_ID = 99
TARGET_TAG_FILTER = [
    {"all": [{"type": "tag", "ids": [TAG_ID]}], "any": None, "none": None}
]
DELIVERED = {"status": "completed", "recipients": 1}


class FakeKit:
    """Answers each Kit call with canned data and records what was called."""

    def __init__(self, member_ids=(OWNER_ID,), stored_filter=TARGET_TAG_FILTER,
                 stored_public=False, owner_found=True, stats=(DELIVERED,),
                 delete_failures=0, forbidden=None):
        self.member_ids = list(member_ids)
        self.stored_filter = stored_filter
        self.stored_public = stored_public
        self.owner_found = owner_found
        self.stats = list(stats)
        self.delete_failures = delete_failures
        self.forbidden = forbidden
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if f"{method} {path}" == self.forbidden:
            raise check.KitError(403, self.forbidden, "no permission")
        if path == "/account":
            return {"account": {
                "plan_type": "newsletter",
                "primary_email_address": OWNER_EMAIL,
                "sending_addresses": [{
                    "email_address": OWNER_EMAIL, "is_default": True,
                    "is_verified": True, "is_dmarc_configured": False,
                }],
            }}
        if path.startswith("/subscribers?per_page"):
            return {"subscribers": [], "pagination": {"total_count": 3}}
        if path.startswith("/subscribers?email_address"):
            return {"subscribers": [{"id": OWNER_ID}] if self.owner_found else []}
        if path.startswith("/tags?"):
            return {"tags": []}
        if (method, path) == ("POST", "/tags"):
            return {"tag": {"id": TAG_ID}}
        if path.startswith(f"/tags/{TAG_ID}/subscribers?"):
            return {"subscribers": [{"id": i} for i in self.member_ids]}
        if (method, path) == ("POST", "/broadcasts"):
            return {"broadcast": {
                "id": BROADCAST_ID, "status": "scheduled",
                "public": self.stored_public,
                "subscriber_filter": self.stored_filter,
            }}
        if path == f"/broadcasts/{BROADCAST_ID}/stats":
            return {"broadcast": {"stats": self.next_stats()}}
        if method == "DELETE" and self.delete_failures > 0:
            self.delete_failures -= 1
            raise check.KitError(500, "DELETE", "try later")
        return None

    def next_stats(self):
        stats = self.stats.pop(0) if len(self.stats) > 1 else self.stats[0]
        if isinstance(stats, Exception):
            raise stats
        return stats

    def was_called(self, method, path):
        return any(c[:2] == (method, path) for c in self.calls)

    def body_of(self, method, path):
        return next(c[2] for c in self.calls if c[:2] == (method, path))


def no_sleep(_seconds):
    pass


class SendTest(unittest.TestCase):
    def test_sends_privately_to_the_tag_when_owner_is_only_member(self):
        kit = FakeKit()
        check.send(kit, NOW, no_sleep)
        body = kit.body_of("POST", "/broadcasts")
        self.assertEqual(body["subscriber_filter"], TARGET_TAG_FILTER)
        self.assertIs(body["public"], False)
        self.assertEqual(body["send_at"], "2026-11-05T00:03:00+00:00")
        self.assertIn("{{ unsubscribe_url }}", body["content"])
        self.assertFalse(kit.was_called("DELETE", f"/broadcasts/{BROADCAST_ID}"))

    def test_refuses_when_tag_has_another_member(self):
        for member_ids in ((OWNER_ID, 8), (8,)):
            with self.subTest(member_ids=member_ids):
                kit = FakeKit(member_ids=member_ids)
                with self.assertRaises(check.UnsafeTarget):
                    check.send(kit, NOW, no_sleep)
                self.assertFalse(kit.was_called("POST", "/broadcasts"))

    def test_refuses_when_tag_stays_empty(self):
        kit = FakeKit(member_ids=())
        with self.assertRaises(check.UnsafeTarget):
            check.send(kit, NOW, no_sleep)
        self.assertFalse(kit.was_called("POST", "/broadcasts"))

    def test_refuses_when_owner_is_not_a_subscriber(self):
        kit = FakeKit(owner_found=False)
        with self.assertRaises(check.UnsafeTarget):
            check.send(kit, NOW, no_sleep)
        self.assertFalse(kit.was_called("POST", "/tags"))

    def test_deletes_broadcast_not_stored_as_requested(self):
        everyone = [{"all": [{"type": "all_subscribers"}], "any": None, "none": None}]
        cases = (
            {"stored_filter": everyone},
            {"stored_filter": []},
            {"stored_filter": None},
            {"stored_public": True},
        )
        for case in cases:
            with self.subTest(case=case):
                kit = FakeKit(**case)
                with self.assertRaises(check.UnsafeTarget):
                    check.send(kit, NOW, no_sleep)
                self.assertTrue(kit.was_called("DELETE", f"/broadcasts/{BROADCAST_ID}"))

    def test_retries_delete_after_a_failure(self):
        kit = FakeKit(stored_filter=[], delete_failures=check.DELETE_ATTEMPTS - 1)
        with self.assertRaisesRegex(check.UnsafeTarget, "deleted it"):
            check.send(kit, NOW, no_sleep)

    def test_says_broadcast_is_live_when_delete_keeps_failing(self):
        kit = FakeKit(stored_filter=[], delete_failures=check.DELETE_ATTEMPTS)
        with self.assertRaisesRegex(check.UnsafeTarget, "STILL SCHEDULED"):
            check.send(kit, NOW, no_sleep)

    def test_any_delete_failure_ends_in_the_warning(self):
        kit = mock.Mock(side_effect=http.client.BadStatusLine("garbled"))
        with self.assertRaisesRegex(check.UnsafeTarget, "STILL SCHEDULED"):
            check.delete_broadcast(kit, BROADCAST_ID, no_sleep)
        self.assertEqual(kit.call_count, check.DELETE_ATTEMPTS)

    def test_fails_when_recipient_count_is_not_one(self):
        for recipients in (0, 2):
            with self.subTest(recipients=recipients):
                kit = FakeKit(stats=[{"status": "completed", "recipients": recipients}])
                with self.assertRaises(RuntimeError):
                    check.send(kit, NOW, no_sleep)


class WaitForDeliveryTest(unittest.TestCase):
    def test_keeps_polling_past_failed_reads_and_pending_states(self):
        kit = FakeKit(stats=[
            OSError("timed out"),
            http.client.IncompleteRead(b""),
            ValueError("not JSON"),
            {"status": "sending", "recipients": 0},
            {"status": "completed", "recipients": 0},
            DELIVERED,
        ])
        self.assertEqual(check.wait_for_delivery(kit, BROADCAST_ID, no_sleep), DELIVERED)

    def test_stops_when_aborted(self):
        aborted = {"status": "aborted", "recipients": 0}
        kit = FakeKit(stats=[aborted])
        self.assertEqual(check.wait_for_delivery(kit, BROADCAST_ID, no_sleep), aborted)
        self.assertEqual(len(kit.calls), 1)

    def test_returns_none_when_every_read_fails(self):
        kit = FakeKit(stats=[OSError("timed out")])
        self.assertIsNone(check.wait_for_delivery(kit, BROADCAST_ID, no_sleep))


class KitRequestTest(unittest.TestCase):
    def test_error_hides_addresses_and_stays_on_one_line(self):
        http_error = urllib.error.HTTPError(
            "url", 422, "bad", {}, io.BytesIO(b"bad\nowner@example.com given"))
        with mock.patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(check.KitError) as caught:
                check.kit_request("key", "GET", "/subscribers?email_address=owner%40example.com")
        self.assertEqual(
            str(caught.exception), "GET /subscribers returned 422: bad [email] given")


class MainTest(unittest.TestCase):
    def run_main(self, mode, kit_request):
        output = io.StringIO()
        with mock.patch("sys.argv", ["kit_send_check.py", mode]), \
                mock.patch.dict(os.environ, {"KIT_API_KEY": "secret-key\n"}), \
                mock.patch.object(check, "kit_request", kit_request), \
                contextlib.redirect_stdout(output):
            code = check.main()
        return code, output.getvalue()

    def run_with_fake(self, mode, kit):
        return self.run_main(mode, lambda _key, *call: kit(*call))

    def test_403_on_broadcast_create_exits_with_the_blocked_code(self):
        kit = FakeKit(forbidden=check.BROADCAST_CREATE_CALL)
        code, output = self.run_with_fake("send", kit)
        self.assertEqual(code, check.EXIT_FORBIDDEN)
        self.assertIn("returned 403", output)

    def test_403_on_another_call_is_an_ordinary_failure(self):
        code, _ = self.run_with_fake("send", FakeKit(forbidden="POST /tags"))
        self.assertEqual(code, 1)

    def test_output_never_contains_an_address_or_the_key(self):
        for mode in ("inspect", "send"):
            with self.subTest(mode=mode):
                code, output = self.run_with_fake(mode, FakeKit())
                self.assertEqual(code, 0)
                self.assertNotIn(OWNER_EMAIL, output)
                self.assertNotIn("secret-key", output)

    def test_unexpected_error_shows_only_its_type(self):
        def explode(*_args):
            raise ValueError("secret-key")

        code, output = self.run_main("inspect", explode)
        self.assertEqual(code, 1)
        self.assertEqual(output, "::error::unexpected ValueError\n")

    def test_strips_whitespace_from_the_key(self):
        seen_keys = []

        def record(key, *call):
            seen_keys.append(key)
            return FakeKit()(*call)

        self.run_main("inspect", record)
        self.assertEqual(set(seen_keys), {"secret-key"})


class InspectTest(unittest.TestCase):
    def test_only_reads(self):
        kit = FakeKit()
        check.inspect(kit)
        self.assertEqual({call[0] for call in kit.calls}, {"GET"})


if __name__ == "__main__":
    unittest.main()
