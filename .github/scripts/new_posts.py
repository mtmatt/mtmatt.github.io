#!/usr/bin/env python3
"""Find posts in the deployed feed that have not been announced by email yet.

This script sends nothing. It reads the feed from the gh-pages branch rather
than the live site, which can lag behind a deploy, and keeps its record on its
own branch, which no build or rerun rewrites.

The record is keyed by URL path, not by the feed's GUID, because a GUID changes
with the domain and that would make every old post look new.
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

FEED_BRANCH = "gh-pages"
FEED_FILE = "index.xml"
RECORD_REF = "refs/heads/newsletter-state"
RECORD_FILE = "announced.json"
ANNOUNCED_PATH_PREFIX = "/blogs/"
# More unannounced posts than this at once means the record or the URLs are wrong.
MAX_NEW_POSTS = 2
# An older unannounced post was most likely renamed, not newly written. Front
# matter dates can precede a post's merge by weeks, so this can't be much shorter.
MAX_POST_AGE = timedelta(days=30)
MAX_SUMMARY_SENTENCES = 3
MAX_SUMMARY_LENGTH = 200
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

    entries maps a post's URL path to {"status": ...}. "seeded" means the post
    predates the newsletter and is never mailed. entries is None, like head,
    until the first seed.
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
    """
    content = json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    blob = git("hash-object", "-w", "--stdin", stdin=content).strip()
    tree = git("mktree", stdin=f"100644 blob {blob}\t{RECORD_FILE}\n").strip()
    parent_args = [] if based_on.head is None else ["-p", based_on.head]
    commit = git(
        "commit-tree", tree, *parent_args, "-m", message,
        env={**os.environ, **BOT_IDENTITY},
    ).strip()
    git("push", "--quiet", "origin", f"{commit}:{RECORD_REF}")


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
    """Return (posts to announce, posts too old to announce)."""
    unannounced = [post for post in posts if post.path not in entries]
    if len(unannounced) > MAX_NEW_POSTS:
        paths = ", ".join(post.path for post in unannounced)
        raise Refused(
            f"{len(unannounced)} unannounced posts, limit is {MAX_NEW_POSTS}; "
            f"check the record and these URLs: {paths}"
        )
    recent = [post for post in unannounced if is_recent(post, now)]
    too_old = [post for post in unannounced if not is_recent(post, now)]
    return recent, too_old


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
    """Build the email body: the summary and a link to the post."""
    # Kit runs Liquid over this, so post text must not look like a template.
    summary = html.escape(summarize(post.summary_html)).replace("{", "&#123;")
    link = html.escape(post.link, quote=True)
    summary_paragraph = f"<p>{summary}</p>" if summary else ""
    return f'{summary_paragraph}<p><a href="{link}">閱讀全文</a></p>'


def print_email(post):
    print(f"would announce: {post.path}")
    print(f"  subject: {post.title}")
    print(f"  body: {email_html(post)}")


def print_new_posts(entries, posts, now):
    """Show what a real run would do, changing nothing."""
    if entries is None:
        print(f"no record yet: seeding would mark {len(posts)} posts as announced")
        return
    recent, too_old = split_new_posts(posts, entries, now)
    print(f"{len(posts)} posts in the feed, {len(entries)} recorded")
    for post in too_old:
        print(f"::warning::not announcing, older than {MAX_POST_AGE.days} days: {post.path}")
    for post in recent:
        print_email(post)
    if not recent:
        print("nothing new to announce")


def seed_record(snapshot, posts):
    """Mark every current post as announced, so none of them is ever mailed."""
    if snapshot.entries is not None:
        raise Refused(f"{RECORD_REF} already holds a record")
    if not posts:
        raise Refused("the feed has no posts, so seeding would protect nothing")
    entries = {post.path: {"status": "seeded"} for post in posts}
    write_record(entries, snapshot, f"seed {len(entries)} existing posts")
    print(f"seeded {len(entries)} posts")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["print", "seed"])
    mode = parser.parse_args().mode

    snapshot = read_record()
    posts = parse_feed(read_feed())
    try:
        if mode == "seed":
            seed_record(snapshot, posts)
        else:
            print_new_posts(snapshot.entries, posts, datetime.now(timezone.utc))
    except Refused as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
