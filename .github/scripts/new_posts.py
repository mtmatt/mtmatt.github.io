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
SITE_NAME = "博陸毅之島"
SITE_URL = "https://mtmatt.csie.org/"
# The same cover on every email: post covers run to 19 MB, too large to mail.
# Published only because it is homepageImage in params.toml.
COVER_IMAGE_URL = f"{SITE_URL}Lac.jpg"
# The white card comes from .github/kit-email-template.html (640px wide, 12px
# corners); these fill it.
CARD_PADDING = "32px"
COVER_STYLE = "display:block;width:100%;height:auto;border:0;border-radius:12px 12px 0 0"
SITE_NAME_STYLE = f"margin:28px {CARD_PADDING} 4px;font-size:14px;color:#6b7280"
SITE_LINK_STYLE = "color:#6b7280;text-decoration:none"
TITLE_STYLE = (
    f"margin:0 {CARD_PADDING} 16px;font-size:30px;font-weight:400;line-height:1.3;"
    "color:#1f2937"
)
TITLE_LINK_STYLE = "color:#1f2937;text-decoration:none"
SUMMARY_STYLE = (
    f"margin:0 {CARD_PADDING} 24px;font-size:16px;line-height:2;"
    "letter-spacing:0.02em;color:#374151"
)
# Padding, not a margin: a bottom margin would collapse through the card.
BUTTON_PARAGRAPH_STYLE = (
    f"margin:0 {CARD_PADDING};padding-bottom:{CARD_PADDING};text-align:center"
)
BUTTON_STYLE = (
    "display:inline-block;padding:12px 24px;background:#23549f;color:#ffffff;"
    "text-decoration:none;border-radius:8px"
)
# Picked by name, so a change to the account's default can't alter this email.
EMAIL_TEMPLATE_NAME = "monterey"
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
# Chinese characters and punctuation, which take no spaces between them.
CJK = "\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef…⋯"
# Elements that don't read as sentences: headings carry a "#" anchor.
UNREADABLE_ELEMENTS = "h[1-6]|table|pre|figure|script|style"
# Tags that sit inside a word or sentence, so removing them must not add a space.
INLINE_TAGS = "a|abbr|b|cite|code|del|em|i|ins|kbd|mark|q|s|small|span|strong|sub|u"
# Hugo builds automatic summaries from these. A written summary has none of them.
BLOCK_TAGS = "p|h[1-6]|blockquote|ul|ol|li|div|table|pre|figure|dl|section|details|center"


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


def is_written_summary(summary_html):
    """Tell a front matter `summary` from Hugo's automatic one.

    The feed doesn't say which it carries, but Hugo emits a one-paragraph written
    summary as bare inline HTML. Several paragraphs count as automatic.
    """
    return not re.search(rf"(?i)</?(?:{BLOCK_TAGS})\b", summary_html)


def plain_text(summary_html):
    prose = re.sub(rf"(?is)<({UNREADABLE_ELEMENTS})\b.*?</\1>", " ", summary_html)
    joined = re.sub(rf"(?i)</?(?:{INLINE_TAGS})\b[^>]*>", "", prose)
    text = html.unescape(re.sub(r"<[^>]+>", " ", joined))
    # A line wrapped in the source isn't a space, unlike one typed between phrases.
    unwrapped = re.sub(rf"(?<=[{CJK}])\s*\n\s*(?=[{CJK}])", "", text)
    return " ".join(unwrapped.split())


def summarize(summary_html):
    """Reduce a feed summary to plain text, of a few sentences unless written by hand."""
    text = plain_text(summary_html)
    if is_written_summary(summary_html):
        return text
    # Removing tags left a space between Chinese paragraphs.
    text = re.sub(rf"(?<=[{CJK}]) (?=[{CJK}])", "", text)
    sentences = re.findall(rf".+?(?:[。！？!?]+{CLOSERS}|\.{CLOSERS}(?=\s)|$)", text)
    summary = ""
    for sentence in sentences[:MAX_SUMMARY_SENTENCES]:
        if summary and len(summary) + len(sentence) > MAX_SUMMARY_LENGTH:
            break
        summary += sentence
    if len(summary) > MAX_SUMMARY_LENGTH:
        summary = summary[:MAX_SUMMARY_LENGTH].rstrip() + "…"
    return summary.strip()


def email_text(text):
    # Kit runs Liquid over the body, so post text must not look like a template.
    return html.escape(text).replace("{", "&#123;")


def cover_and_title(title_text, link):
    cover = f'<img src="{COVER_IMAGE_URL}" alt="{SITE_NAME}" width="640" style="{COVER_STYLE}">'
    site_name = f'<a href="{SITE_URL}" style="{SITE_LINK_STYLE}">{SITE_NAME}</a>'
    title = f'<a href="{link}" style="{TITLE_LINK_STYLE}">{email_text(title_text)}</a>'
    return (
        f'<a href="{SITE_URL}">{cover}</a>'
        f'<p style="{SITE_NAME_STYLE}">{site_name}</p>'
        f'<h1 style="{TITLE_STYLE}">{title}</h1>'
    )


def read_more_button(link):
    button = f'<a href="{link}" style="{BUTTON_STYLE}">閱讀全文</a>'
    return f'<p style="{BUTTON_PARAGRAPH_STYLE}">{button}</p>'


def email_html(post):
    """Build the email body: the site cover, the title, the summary and a link.

    Styles are inline because email clients drop style sheets. The unsubscribe
    link and postal address come from the template, which find_email_template
    checks before anything is sent.
    """
    link = html.escape(post.link, quote=True)
    summary = email_text(summarize(post.summary_html))
    summary_paragraph = f'<p style="{SUMMARY_STYLE}">{summary}</p>' if summary else ""
    return f"{cover_and_title(post.title, link)}{summary_paragraph}{read_more_button(link)}"


def broadcast_body(post, template_id, now, send_at):
    """Return the Kit broadcast for a post, leaving its audience to the caller."""
    return {
        "subject": post.title,
        "preview_text": summarize(post.summary_html).replace("{", "")[:MAX_PREVIEW_LENGTH],
        "description": f"newsletter {post.path}",
        "content": email_html(post),
        "email_template_id": template_id,
        "public": False,
        "published_at": now.isoformat(),
        "send_at": send_at,
    }


def ensure_subject_is_plain(post):
    """Refuse a title that Kit could read as a template, since it can't be escaped."""
    if "{{" in post.title or "{%" in post.title:
        raise Refused(f"the title of {post.path} looks like a Kit template")


def find_email_template(api):
    """Return the id of the email template, refusing one without an unsubscribe link.

    A Starting point template holds the whole email, so this script's body would
    have no footer. Kit could accept that and then never send it.
    """
    templates = api("GET", "/email_templates")["email_templates"]
    template = next(
        (t for t in templates if t["name"].casefold() == EMAIL_TEMPLATE_NAME.casefold()),
        None,
    )
    if template is None:
        raise Refused(f"Kit has no email template named {EMAIL_TEMPLATE_NAME}")
    if template["category"] == STARTING_POINT_TEMPLATE:
        raise Refused(
            f"the {EMAIL_TEMPLATE_NAME} template is a {STARTING_POINT_TEMPLATE} one, "
            "so it won't add an unsubscribe link"
        )
    print(f"email template category: {template['category']}", flush=True)
    return template["id"]


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
    template_id = find_email_template(api)
    for post in to_announce:
        ensure_subject_is_plain(post)
    for post in to_announce:
        snapshot = set_status(snapshot, post.path, SENDING)
        body = broadcast_body(post, template_id, now, (now + SEND_DELAY).isoformat())
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
    template_id = find_email_template(api)
    draft_body = broadcast_body(post, template_id, clock(), None)
    draft = api("POST", "/broadcasts", draft_body)["broadcast"]
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
        **broadcast_body(post, template_id, now, (now + TEST_SEND_DELAY).isoformat()),
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
