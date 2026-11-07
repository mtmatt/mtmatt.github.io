#!/usr/bin/env python3
"""Announce posts from the deployed feed by email through Kit.

The feed comes from the gh-pages branch rather than the live site, which can
lag behind a deploy. The record of announced posts lives on its own branch,
which no build or rerun rewrites, and is keyed by URL path, not by the feed's
GUID, because a GUID changes with the domain and that would make every old post
look new.

Sending can't be recalled, so a post is claimed in the record before Kit is
called. After a failure the post may or may not have gone out, so it stays
flagged and is never resent without a person checking Kit first.
This repository is public and so are its Actions logs: never print the API key
or an email address.
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import kit_send_check
from kit_send_check import KitError, UnsafeTarget

FEED_BRANCH = "gh-pages"
FEED_FILE = "index.xml"
RECORD_REF = "refs/heads/newsletter-state"
RECORD_FILE = "announced.json"
ANNOUNCED_PATH_PREFIX = "/blogs/"
# More unannounced posts than this at once means the record or the URLs are wrong.
MAX_NEW_POSTS = 2
# An older unannounced post was most likely renamed, not newly written. Front
# matter dates can precede a post's merge by weeks, so this can't be much shorter.
# Renaming a post younger than this makes it look new, so URLs must stay put.
MAX_POST_AGE = timedelta(days=30)
MAX_SUMMARY_SENTENCES = 3
MAX_SUMMARY_LENGTH = 200
MAX_PREVIEW_LENGTH = 100
STARTING_POINT_TEMPLATE = "Starting point"
# Gives Pages time to publish the post, and a window to cancel in Kit.
SEND_DELAY = timedelta(minutes=10)
TEST_SEND_DELAY = timedelta(minutes=3)
SEEDED = "seeded"  # Predates the newsletter, never mailed.
APPROVED = "approved"  # A person cleared a held-back post for the next send.
DISMISSED = "dismissed"  # A person decided not to announce this post.
SENDING = "sending"  # Claimed before calling Kit; still here means the outcome is unknown.
SCHEDULED = "scheduled"  # Kit accepted the broadcast.
BOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "github-actions[bot]",
    "GIT_AUTHOR_EMAIL": "github-actions[bot]@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "github-actions[bot]",
    "GIT_COMMITTER_EMAIL": "github-actions[bot]@users.noreply.github.com",
}
MISSING_REMOTE_REF_EXIT_CODE = 2
# Closing quotes and brackets stay with the sentence they end.
CLOSERS = "[」』”\"）)]*"


class Refused(Exception):
    """This run must not continue, because something looks wrong."""


@dataclass(frozen=True)
class Post:
    path: str
    title: str
    link: str
    published: datetime
    summary_html: str


@dataclass(frozen=True)
class RecordSnapshot:
    """The record as read, and the commit to build the next write on.

    entries maps a post's URL path to {"status": ...}, using the status
    constants above. entries is None, like head, until the first seed.
    """

    head: str | None
    entries: dict | None


def git(*args, stdin=None, env=None):
    """Run git, returning its stdout and raising if it fails.

    stderr is left to reach the log, so a failed run says why.
    """
    result = subprocess.run(
        ["git", *args], input=stdin, env=env, text=True,
        stdout=subprocess.PIPE, check=True,
    )
    return result.stdout


def read_feed():
    git("fetch", "--quiet", "--depth=1", "origin", FEED_BRANCH)
    return git("show", f"FETCH_HEAD:{FEED_FILE}")


def read_record():
    """Return a RecordSnapshot of the record branch."""
    try:
        heads = git("ls-remote", "--exit-code", "origin", RECORD_REF)
    except subprocess.CalledProcessError as error:
        if error.returncode == MISSING_REMOTE_REF_EXIT_CODE:
            return RecordSnapshot(head=None, entries=None)
        raise
    head = heads.split()[0]
    git("fetch", "--quiet", "origin", RECORD_REF)
    return RecordSnapshot(head, json.loads(git("show", f"{head}:{RECORD_FILE}")))


def write_record(entries, based_on, message):
    """Commit entries straight to the record branch, without any checkout.

    The new commit's parent is the snapshot the caller read, so the push only
    fast-forwards if nobody wrote since then. A concurrent run fails here
    instead of overwriting what the other run recorded.

    Returns the snapshot to base the next write on.
    """
    content = json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    blob = git("hash-object", "-w", "--stdin", stdin=content).strip()
    tree = git("mktree", stdin=f"100644 blob {blob}\t{RECORD_FILE}\n").strip()
    parent_args = [] if based_on.head is None else ["-p", based_on.head]
    # The run id keeps two runs' identical claims from being one commit, which
    # would make the second push a silent success.
    run_id = f"run {os.environ.get('GITHUB_RUN_ID')}.{os.environ.get('GITHUB_RUN_ATTEMPT')}"
    commit = git(
        "commit-tree", tree, *parent_args, "-m", message, "-m", run_id,
        env={**os.environ, **BOT_IDENTITY},
    ).strip()
    git("push", "--quiet", "origin", f"{commit}:{RECORD_REF}")
    return RecordSnapshot(commit, entries)


def set_status(snapshot, path, status, **details):
    """Record a post's new status and return the updated snapshot."""
    entries = {**snapshot.entries, path: {"status": status, **details}}
    return write_record(entries, snapshot, f"{status} {path}")


def parse_feed(feed_xml):
    """Return the feed's announceable posts."""
    posts = []
    for item in ElementTree.fromstring(feed_xml).iter("item"):
        link = item.findtext("link")
        path = urlparse(link).path
        if path.startswith(ANNOUNCED_PATH_PREFIX):
            posts.append(Post(
                path=path,
                title=item.findtext("title"),
                link=link,
                published=parsedate_to_datetime(item.findtext("pubDate")),
                summary_html=item.findtext("description") or "",
            ))
    return posts


def is_recent(post, now):
    return now - post.published <= MAX_POST_AGE


def split_new_posts(posts, entries, now):
    """Return (posts to announce, posts too old to announce).

    Approved posts are announced whatever their age: a person looked at them.
    """
    paths = [post.path for post in posts]
    if len(paths) != len(set(paths)):
        raise Refused("the feed lists a post twice")
    unannounced = [post for post in posts if post.path not in entries]
    if len(unannounced) > MAX_NEW_POSTS:
        listed = ", ".join(post.path for post in unannounced)
        raise Refused(
            f"{len(unannounced)} unannounced posts, limit is {MAX_NEW_POSTS}; "
            f"check the record and these URLs: {listed}"
        )
    approved = [post for post in posts if status_of(entries, post.path) == APPROVED]
    recent = [post for post in unannounced if is_recent(post, now)]
    too_old = [post for post in unannounced if not is_recent(post, now)]
    to_announce = recent + approved
    return to_announce, too_old


def status_of(entries, path):
    return entries.get(path, {}).get("status")


def warn_about_unresolved(entries, too_old):
    """Flag posts that need a person, on every run until someone decides."""
    for post in too_old:
        print(f"::warning::not announcing, older than {MAX_POST_AGE.days} days: {post.path}")
    for path in sorted(entries):
        if status_of(entries, path) == SENDING:
            print(f"::warning::send was not confirmed, check Kit, then resolve: {path}")


def summarize(summary_html):
    """Reduce a feed summary to its first few sentences of plain text."""
    # Headings carry a "#" anchor, and tables and code don't read as sentences.
    prose = re.sub(
        r"(?is)<(h[1-6]|table|pre|figure|script|style)\b.*?</\1>", " ", summary_html
    )
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", prose)).split())
    sentences = re.findall(rf".+?(?:[。！？!?]+{CLOSERS}|\.{CLOSERS}(?=\s)|$)", text)
    summary = ""
    for sentence in sentences[:MAX_SUMMARY_SENTENCES]:
        if summary and len(summary) + len(sentence) > MAX_SUMMARY_LENGTH:
            break
        summary += sentence
    if len(summary) > MAX_SUMMARY_LENGTH:
        summary = summary[:MAX_SUMMARY_LENGTH].rstrip() + "…"
    return summary.strip()


def email_html(post):
    """Build the email body: the summary and a link to the post.

    The unsubscribe link and postal address come from the account's default
    template, which ensure_template_adds_footer checks before anything is sent.
    """
    # Kit runs Liquid over this, so post text must not look like a template.
    summary = html.escape(summarize(post.summary_html)).replace("{", "&#123;")
    link = html.escape(post.link, quote=True)
    summary_paragraph = f"<p>{summary}</p>" if summary else ""
    return f'{summary_paragraph}<p><a href="{link}">閱讀全文</a></p>'


def broadcast_body(post, now, send_at):
    """Return the Kit broadcast for a post, leaving its audience to the caller."""
    return {
        "subject": post.title,
        "preview_text": summarize(post.summary_html).replace("{", "")[:MAX_PREVIEW_LENGTH],
        "description": f"newsletter {post.path}",
        "content": email_html(post),
        "public": False,
        "published_at": now.isoformat(),
        "send_at": send_at,
    }


def ensure_subject_is_plain(post):
    """Refuse a title that Kit could read as a template, since it can't be escaped."""
    if "{{" in post.title or "{%" in post.title:
        raise Refused(f"the title of {post.path} looks like a Kit template")


def ensure_template_adds_footer(api):
    """Refuse a default template that won't add the required unsubscribe link.

    A Starting point template holds the whole email, so this script's body would
    have no footer. Kit could accept that and then never send it.
    """
    templates = api("GET", "/email_templates")["email_templates"]
    default = next((t for t in templates if t["is_default"]), None)
    if default is None or default["category"] == STARTING_POINT_TEMPLATE:
        raise Refused("the default Kit email template won't add an unsubscribe link")
    print(f"default template category: {default['category']}", flush=True)


def print_email(post):
    print(f"would announce: {post.path}")
    print(f"  subject: {post.title}")
    print(f"  body: {email_html(post)}")


def print_new_posts(entries, posts, now):
    """Show what a real run would do, changing nothing."""
    if entries is None:
        print(f"no record yet: seeding would mark {len(posts)} posts as announced")
        return
    to_announce, too_old = split_new_posts(posts, entries, now)
    print(f"{len(posts)} posts in the feed, {len(entries)} recorded")
    warn_about_unresolved(entries, too_old)
    for post in to_announce:
        print_email(post)
    if not to_announce:
        print("nothing new to announce")


def send_new_posts(snapshot, posts, api, now):
    """Schedule a broadcast to all subscribers for each new post."""
    if snapshot.entries is None:
        raise Refused("no record yet, so every post would look new; seed first")
    to_announce, too_old = split_new_posts(posts, snapshot.entries, now)
    warn_about_unresolved(snapshot.entries, too_old)
    if not to_announce:
        print("nothing new to announce")
        return
    ensure_template_adds_footer(api)
    for post in to_announce:
        ensure_subject_is_plain(post)
    for post in to_announce:
        snapshot = set_status(snapshot, post.path, SENDING)
        body = broadcast_body(post, now, (now + SEND_DELAY).isoformat())
        broadcast = api("POST", "/broadcasts", body)["broadcast"]
        broadcast_id = broadcast["id"]
        # Printed before the next write: if that fails, this mail is still going out.
        print(
            f"scheduled broadcast {broadcast_id} for {post.path}, "
            f"audience: {json.dumps(broadcast.get('subscriber_filter'))}",
            flush=True,
        )
        if broadcast.get("public") is not False:
            kit_send_check.delete_broadcast(api, broadcast_id)
            raise UnsafeTarget(f"broadcast {broadcast_id} would be published; deleted it")
        snapshot = set_status(snapshot, post.path, SCHEDULED, broadcast_id=broadcast_id)


def utc_now():
    return datetime.now(timezone.utc)


def send_test(posts, api, clock=utc_now, sleep=time.sleep):
    """Send the latest post to the account owner alone, recording nothing.

    It first saves and deletes a draft with no audience, because Kit's docs both
    require subscriber_filter and say leaving it out means everyone. A draft
    can't send, so this shows how a real announcement would be stored.
    """
    if not posts:
        raise Refused("the feed has no posts to test with")
    post = posts[0]
    ensure_subject_is_plain(post)
    ensure_template_adds_footer(api)
    draft = api("POST", "/broadcasts", broadcast_body(post, clock(), None))["broadcast"]
    print(
        f"draft {draft['id']} audience: {json.dumps(draft.get('subscriber_filter'))}, "
        f"template: {json.dumps(draft.get('email_template'))}",
        flush=True,
    )
    # No retries or alarm like delete_broadcast's: a leftover draft can't send.
    api("DELETE", f"/broadcasts/{draft['id']}")

    tag_id = kit_send_check.owner_only_tag(api, sleep)
    # Taken after the tag check, which can wait for minutes.
    now = clock()
    body = {
        **broadcast_body(post, now, (now + TEST_SEND_DELAY).isoformat()),
        "subject": f"[測試] {post.title}",
        "subscriber_filter": [
            {"all": [{"type": "tag", "ids": [tag_id]}], "any": None, "none": None}
        ],
    }
    broadcast = api("POST", "/broadcasts", body)["broadcast"]
    broadcast_id = broadcast["id"]
    if not kit_send_check.is_private_to_tag(broadcast, tag_id):
        kit_send_check.delete_broadcast(api, broadcast_id, sleep)
        raise UnsafeTarget(f"broadcast {broadcast_id} was not stored as requested; deleted it")
    stats = kit_send_check.wait_for_delivery(api, broadcast_id, sleep)
    if stats is None or stats["status"] != "completed" or stats["recipients"] != 1:
        raise RuntimeError(f"broadcast {broadcast_id} did not deliver to exactly one")
    print(f"test of {post.path} delivered to 1 recipient")


def resolve_post(snapshot, posts, path, status):
    """Record a person's decision about a held-back or unconfirmed post."""
    if snapshot.entries is None:
        raise Refused("no record yet; seed first")
    current = status_of(snapshot.entries, path)
    open_statuses = (None, SENDING, APPROVED) if status == DISMISSED else (None, SENDING)
    if current not in open_statuses:
        raise Refused(f"{path} is already {current}")
    # A recorded post can leave the feed and still need dismissing. Anything else
    # outside the feed is a mistyped path, which must not look like a success.
    in_feed = path in [post.path for post in posts]
    if not in_feed and (status == APPROVED or current is None):
        raise Refused(f"{path} is not in the feed")
    set_status(snapshot, path, status)
    print(f"{path} is now {status}")


def seed_record(snapshot, posts):
    """Mark every current post as announced, so none of them is ever mailed."""
    if snapshot.entries is not None:
        raise Refused(f"{RECORD_REF} already holds a record")
    if not posts:
        raise Refused("the feed has no posts, so seeding would protect nothing")
    entries = {post.path: {"status": SEEDED} for post in posts}
    write_record(entries, snapshot, f"seed {len(entries)} existing posts")
    print(f"seeded {len(entries)} posts")


def run(mode, path, api):
    snapshot = read_record()
    posts = parse_feed(read_feed())
    now = utc_now()
    if mode == "print":
        print_new_posts(snapshot.entries, posts, now)
    elif mode == "seed":
        seed_record(snapshot, posts)
    elif mode == "test":
        send_test(posts, api)
    elif mode == "send":
        if os.environ.get("SEND_ENABLED") != "true":
            raise Refused("sending is off; set the NEWSLETTER_SEND variable to true")
        send_new_posts(snapshot, posts, api, now)
    else:
        resolve_post(snapshot, posts, path, {"approve": APPROVED, "dismiss": DISMISSED}[mode])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["print", "seed", "test", "send", "approve", "dismiss"])
    parser.add_argument("--path", help="the post's URL path, for approve and dismiss")
    args = parser.parse_args()
    api_key = os.environ.get("KIT_API_KEY", "").strip()

    def api(method, path, body=None):
        return kit_send_check.kit_request(api_key, method, path, body)

    try:
        run(args.mode, args.path, api)
    except (Refused, KitError, UnsafeTarget, RuntimeError) as error:
        print(f"::error::{error}")
        return 1
    except Exception as error:
        # A traceback could quote the key or an address, so show only the type.
        print(f"::error::unexpected {type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
