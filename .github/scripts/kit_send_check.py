#!/usr/bin/env python3
"""Check whether this Kit account can send a broadcast through the API.

The inspect mode only reads. The send mode sends one real email to the account
owner, so it refuses to run unless the target tag contains that subscriber and
nobody else. This repository is public and so are its Actions logs: never print
the API key or an email address.
"""

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE = "https://api.kit.com/v4"
TEST_TAG_NAME = "api-send-check"
# Kit documents no "send now" option, only a scheduled send_at. The lead time
# also leaves room to delete the broadcast if its stored filter looks wrong.
SEND_DELAY = timedelta(minutes=3)
# Kit documents tag lists lagging by up to five minutes.
TAG_READ_ATTEMPTS = 22
TAG_READ_INTERVAL_SECONDS = 15
DELETE_ATTEMPTS = 5
DELETE_RETRY_SECONDS = 10
STATS_POLL_ATTEMPTS = 40
STATS_POLL_INTERVAL_SECONDS = 30

BROADCAST_CREATE_CALL = "POST /broadcasts"
EXIT_FORBIDDEN = 3
MAX_ERROR_DETAIL_LENGTH = 300


class KitError(Exception):
    """A non-2xx response; the message is safe for public logs on one line."""

    def __init__(self, status, call, detail):
        one_line_detail = " ".join(redact_emails(detail).split())
        super().__init__(
            f"{call} returned {status}: {one_line_detail[:MAX_ERROR_DETAIL_LENGTH]}"
        )
        self.status = status
        self.call = call


class UnsafeTarget(Exception):
    """The send was refused because it could reach someone else."""


def redact_emails(text):
    return re.sub(r"\S+@\S+", "[email]", text)


def kit_request(api_key, method, path, body=None):
    """Call the Kit API and return the decoded JSON body (None when empty)."""
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "X-Kit-Api-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "mtmatt-site-kit-send-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        # The query string is left out because it can hold an email address.
        call = f"{method} {path.split('?')[0]}"
        detail = error.read().decode(errors="replace")
        raise KitError(error.code, call, detail) from None
    return json.loads(payload) if payload else None


def find_owner_subscriber(api, owner_email):
    """Return the owner's active subscriber record, or None if there isn't one."""
    query = urllib.parse.urlencode({"email_address": owner_email, "status": "active"})
    matches = api("GET", f"/subscribers?{query}")["subscribers"]
    return matches[0] if len(matches) == 1 else None


def inspect(api):
    """Report what a send would use, changing nothing in Kit."""
    account = api("GET", "/account")["account"]
    owner_email = account["primary_email_address"]
    senders = account.get("sending_addresses", [])
    default_sender = next((s for s in senders if s["is_default"]), None)
    owner = find_owner_subscriber(api, owner_email)
    subscribers = api("GET", "/subscribers?per_page=1&include_total_count=true")
    tags = api("GET", "/tags?per_page=1000&include=subscriber_count")["tags"]
    test_tag = next((t for t in tags if t["name"] == TEST_TAG_NAME), None)

    print(f"plan_type: {account['plan_type']}")
    print(f"active subscribers: {subscribers['pagination'].get('total_count')}")
    print(f"owner is an active subscriber: {owner is not None}")
    if default_sender is None:
        print("default sending address: none")
    else:
        print(f"default sender is the owner's address: "
              f"{default_sender['email_address'] == owner_email}")
        print(f"default sender verified: {default_sender['is_verified']}")
        print(f"default sender DMARC configured: {default_sender['is_dmarc_configured']}")
    print(f"tag {TEST_TAG_NAME!r}: "
          f"{'absent' if test_tag is None else test_tag['subscriber_count']}")


def ensure_only_member(api, tag_id, subscriber_id, sleep=time.sleep):
    """Raise UnsafeTarget unless the tag contains exactly this subscriber."""
    # A tag applied just now may not be listed yet, so an empty list is retried.
    # An unexpected member never is. The same lag could hide someone tagged in
    # the last few minutes, which is why this tag is used by nothing else.
    for attempt in range(TAG_READ_ATTEMPTS):
        members = api("GET", f"/tags/{tag_id}/subscribers?status=all&per_page=2")
        member_ids = [member["id"] for member in members["subscribers"]]
        if member_ids == [subscriber_id]:
            return
        if member_ids:
            raise UnsafeTarget(f"tag {tag_id} has someone other than the owner")
        if attempt < TAG_READ_ATTEMPTS - 1:
            sleep(TAG_READ_INTERVAL_SECONDS)
    raise UnsafeTarget(f"tag {tag_id} never listed the owner")


def broadcast_body(tag_id, now):
    return {
        "subject": "Kit API send check",
        "preview_text": "API send check",
        "description": "API send check",
        # A Starting point template holds the whole email, including these tags,
        # and Kit won't send one that has no unsubscribe link.
        "content": (
            "<p>Test from the kit-send-check GitHub Actions workflow.</p>"
            '<p><a href="{{ unsubscribe_url }}">Unsubscribe</a><br>{{ address }}</p>'
        ),
        "public": False,
        "published_at": now.isoformat(),
        "send_at": (now + SEND_DELAY).isoformat(),
        "subscriber_filter": [
            {"all": [{"type": "tag", "ids": [tag_id]}], "any": None, "none": None}
        ],
    }


def is_private_to_tag(broadcast, tag_id):
    """Compare by equality so that any unexpected shape counts as unsafe."""
    group = {"all": [{"type": "tag", "ids": [tag_id]}]}
    accepted_filters = ([group], [{**group, "any": None, "none": None}])
    return (
        broadcast.get("public") is False
        and broadcast.get("subscriber_filter") in accepted_filters
    )


def delete_broadcast(api, broadcast_id, sleep=time.sleep):
    """Delete a scheduled broadcast, which is about to go out if this fails."""
    for attempt in range(DELETE_ATTEMPTS):
        try:
            api("DELETE", f"/broadcasts/{broadcast_id}")
            return
        except Exception as error:  # Any failure must end in the warning below.
            print(f"delete attempt {attempt + 1} failed: {type(error).__name__}", flush=True)
            if attempt < DELETE_ATTEMPTS - 1:
                sleep(DELETE_RETRY_SECONDS)
    raise UnsafeTarget(
        f"broadcast {broadcast_id} IS STILL SCHEDULED: cancel it in Kit now"
    )


def wait_for_delivery(api, broadcast_id, sleep=time.sleep):
    """Return the last stats seen, or None if every read failed."""
    stats = None
    for attempt in range(STATS_POLL_ATTEMPTS):
        try:
            stats = api("GET", f"/broadcasts/{broadcast_id}/stats")["broadcast"]["stats"]
            print(f"status={stats['status']} recipients={stats['recipients']}", flush=True)
            if stats["status"] == "aborted":
                break
            # The recipient count can trail the status, so wait until it is set.
            if stats["status"] == "completed" and stats["recipients"] > 0:
                break
        except (KitError, OSError, http.client.HTTPException, ValueError) as error:
            # The email goes out regardless, so a failed read must not end the check.
            print(f"stats read failed: {type(error).__name__}", flush=True)
        if attempt < STATS_POLL_ATTEMPTS - 1:
            sleep(STATS_POLL_INTERVAL_SECONDS)
    return stats


def owner_only_tag(api, sleep=time.sleep):
    """Return the id of a tag that reaches the account owner and nobody else."""
    owner_email = api("GET", "/account")["account"]["primary_email_address"]
    owner = find_owner_subscriber(api, owner_email)
    if owner is None:
        raise UnsafeTarget("the account owner is not an active subscriber")

    tag_id = api("POST", "/tags", {"name": TEST_TAG_NAME})["tag"]["id"]
    api("POST", f"/tags/{tag_id}/subscribers", {"email_address": owner_email})
    ensure_only_member(api, tag_id, owner["id"], sleep)
    return tag_id


def send(api, now=None, sleep=time.sleep):
    """Send one real email to the account owner and confirm it was delivered."""
    tag_id = owner_only_tag(api, sleep)
    body = broadcast_body(tag_id, now or datetime.now(timezone.utc))
    print("creating broadcast; if this step fails, check Kit for a scheduled one", flush=True)
    broadcast = api("POST", "/broadcasts", body)["broadcast"]
    broadcast_id = broadcast["id"]
    print(f"broadcast {broadcast_id} created, status={broadcast.get('status')}", flush=True)
    if not is_private_to_tag(broadcast, tag_id):
        delete_broadcast(api, broadcast_id, sleep)
        raise UnsafeTarget(f"broadcast {broadcast_id} was not stored as requested; deleted it")

    stats = wait_for_delivery(api, broadcast_id, sleep)
    if stats is None or stats["status"] != "completed" or stats["recipients"] != 1:
        raise RuntimeError(f"broadcast {broadcast_id} did not deliver to exactly one")
    print(f"broadcast {broadcast_id} delivered to 1 recipient")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["inspect", "send"])
    mode = parser.parse_args().mode
    api_key = os.environ["KIT_API_KEY"].strip()

    def api(method, path, body=None):
        return kit_request(api_key, method, path, body)

    try:
        if mode == "send":
            send(api)
        else:
            inspect(api)
    except KitError as error:
        print(f"::error::{error}")
        blocked = error.status == 403 and error.call == BROADCAST_CREATE_CALL
        return EXIT_FORBIDDEN if blocked else 1
    except (UnsafeTarget, RuntimeError) as error:
        print(f"::error::{error}")
        return 1
    except Exception as error:
        # A traceback could quote the key or an address, so show only the type.
        print(f"::error::unexpected {type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
