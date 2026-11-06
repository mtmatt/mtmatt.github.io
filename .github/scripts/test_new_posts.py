import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import new_posts

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


def feed(*items):
    entries = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>{date}</pubDate><description>{description}</description></item>"
        for title, link, date, description in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{entries}</channel></rss>'


def post(path, age=timedelta(days=1), summary_html="<p>Hello.</p>"):
    return new_posts.Post(
        path=path,
        title="Title",
        link=f"https://mtmatt.csie.org{path}",
        published=NOW - age,
        summary_html=summary_html,
    )


class ParseFeedTest(unittest.TestCase):
    def test_reads_posts_and_decodes_the_escaped_summary(self):
        xml = feed((
            "多一雙手", "https://mtmatt.csie.org/blogs/hands/",
            "Thu, 29 Oct 2026 18:46:41 +0800", "&lt;p&gt;第一段&lt;/p&gt;",
        ))
        [parsed] = new_posts.parse_feed(xml)
        self.assertEqual(parsed.path, "/blogs/hands/")
        self.assertEqual(parsed.title, "多一雙手")
        self.assertEqual(parsed.summary_html, "<p>第一段</p>")
        self.assertEqual(parsed.published.utcoffset(), timedelta(hours=8))

    def test_leaves_out_pages_that_are_not_blog_posts(self):
        xml = feed((
            "OwoJudge", "https://mtmatt.csie.org/projects/owojudge/",
            "Fri, 24 Oct 2025 00:00:00 +0000", "x",
        ))
        self.assertEqual(new_posts.parse_feed(xml), [])

    def test_the_same_post_keeps_its_key_across_domains(self):
        date = "Thu, 29 Oct 2026 18:46:41 +0800"
        old = feed(("A", "https://mtmatt.github.io/blogs/a/", date, "x"))
        new = feed(("A", "https://mtmatt.csie.org/blogs/a/", date, "x"))
        self.assertEqual(
            new_posts.parse_feed(old)[0].path, new_posts.parse_feed(new)[0].path
        )


class SplitNewPostsTest(unittest.TestCase):
    def test_recorded_posts_are_never_new(self):
        entries = {"/blogs/a/": {"status": "seeded"}}
        self.assertEqual(new_posts.split_new_posts([post("/blogs/a/")], entries, NOW), ([], []))

    def test_returns_an_unrecorded_post(self):
        recent, too_old = new_posts.split_new_posts([post("/blogs/new/")], {}, NOW)
        self.assertEqual([p.path for p in recent], ["/blogs/new/"])
        self.assertEqual(too_old, [])

    def test_a_post_merged_weeks_after_its_date_still_counts_as_recent(self):
        dated_early = post("/blogs/intern/", age=timedelta(days=17))
        self.assertEqual(new_posts.split_new_posts([dated_early], {}, NOW), ([dated_early], []))

    def test_an_old_unrecorded_post_is_held_back(self):
        renamed = post("/blogs/renamed/", age=new_posts.MAX_POST_AGE + timedelta(days=1))
        self.assertEqual(new_posts.split_new_posts([renamed], {}, NOW), ([], [renamed]))

    def test_age_is_compared_across_timezones(self):
        offset = timezone(timedelta(hours=8))
        boundary = NOW.astimezone(offset) - new_posts.MAX_POST_AGE
        at_limit = new_posts.Post("/blogs/a/", "T", "https://x/blogs/a/", boundary, "")
        self.assertTrue(new_posts.is_recent(at_limit, NOW))

    def test_refuses_more_new_posts_than_the_limit(self):
        posts = [post(f"/blogs/{i}/") for i in range(new_posts.MAX_NEW_POSTS + 1)]
        with self.assertRaisesRegex(new_posts.Refused, "/blogs/0/, /blogs/1/"):
            new_posts.split_new_posts(posts, {}, NOW)

    def test_a_mass_rename_of_old_posts_is_refused_too(self):
        old = new_posts.MAX_POST_AGE + timedelta(days=1)
        posts = [post(f"/blogs/{i}/", age=old) for i in range(new_posts.MAX_NEW_POSTS + 1)]
        with self.assertRaises(new_posts.Refused):
            new_posts.split_new_posts(posts, {}, NOW)


class SummarizeTest(unittest.TestCase):
    def test_drops_headings_with_their_anchor(self):
        summary_html = '<h2 class="group">第一章<a href="#x">#</a></h2><p>綠燈沒有亮。</p>'
        self.assertEqual(new_posts.summarize(summary_html), "綠燈沒有亮。")

    def test_drops_tables(self):
        summary_html = "<table><tr><td>2025/11</td></tr></table><p>Intro.</p>"
        self.assertEqual(new_posts.summarize(summary_html), "Intro.")

    def test_keeps_at_most_three_sentences(self):
        summary_html = "<p>一。二。三。四。</p>"
        self.assertEqual(new_posts.summarize(summary_html), "一。二。三。")

    def test_english_sentences_end_at_a_period_and_space(self):
        summary_html = "<p>Version 6.3 is out. It works. Really. Four.</p>"
        self.assertEqual(new_posts.summarize(summary_html), "Version 6.3 is out. It works. Really.")

    def test_stops_before_a_sentence_that_would_be_too_long(self):
        summary_html = f"<p>短句。{'長' * new_posts.MAX_SUMMARY_LENGTH}。</p>"
        self.assertEqual(new_posts.summarize(summary_html), "短句。")

    def test_cuts_a_single_long_sentence(self):
        summary = new_posts.summarize(f"<p>{'長' * 500}</p>")
        self.assertEqual(len(summary), new_posts.MAX_SUMMARY_LENGTH + 1)
        self.assertTrue(summary.endswith("…"))

    def test_a_closing_quote_stays_with_its_sentence(self):
        summary = new_posts.summarize("<p>他說：「你好。」她笑了。</p>")
        self.assertEqual(summary, "他說：「你好。」她笑了。")

    def test_decodes_entities(self):
        self.assertEqual(new_posts.summarize("<p>A &amp; B.</p>"), "A & B.")

    def test_is_empty_when_there_is_no_prose(self):
        self.assertEqual(new_posts.summarize("<h2>Only a heading</h2>"), "")


class EmailHtmlTest(unittest.TestCase):
    def test_has_the_summary_and_the_link(self):
        body = new_posts.email_html(post("/blogs/a/"))
        self.assertIn("<p>Hello.</p>", body)
        self.assertIn('href="https://mtmatt.csie.org/blogs/a/"', body)

    def test_omits_an_empty_summary(self):
        body = new_posts.email_html(post("/blogs/a/", summary_html="<h2>x</h2>"))
        self.assertTrue(body.startswith("<p><a "))

    def test_post_text_cannot_inject_markup_or_liquid(self):
        body = new_posts.email_html(post(
            "/blogs/a/", summary_html="<p>&lt;script&gt; {{ subscriber.email }} {% x %}.</p>"
        ))
        self.assertNotIn("<script>", body)
        self.assertNotIn("{{", body)
        self.assertNotIn("{%", body)


class PrintNewPostsTest(unittest.TestCase):
    def output_of(self, entries, posts):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            new_posts.print_new_posts(entries, posts, NOW)
        return output.getvalue()

    def test_before_seeding_nothing_is_offered_for_announcing(self):
        output = self.output_of(None, [post("/blogs/a/")])
        self.assertIn("seeding would mark 1 posts", output)
        self.assertNotIn("would announce", output)

    def test_shows_the_email_for_a_new_post(self):
        output = self.output_of({}, [post("/blogs/new/")])
        self.assertIn("would announce: /blogs/new/", output)
        self.assertIn("subject: Title", output)

    def test_says_when_there_is_nothing_new(self):
        output = self.output_of({"/blogs/a/": {}}, [post("/blogs/a/")])
        self.assertIn("nothing new", output)

    def test_a_held_back_post_raises_a_visible_warning(self):
        old = post("/blogs/old/", age=new_posts.MAX_POST_AGE + timedelta(days=1))
        output = self.output_of({}, [old])
        self.assertIn("::warning::not announcing", output)
        self.assertNotIn("would announce", output)


class MainTest(unittest.TestCase):
    def run_main(self, mode, snapshot, feed_xml):
        output = io.StringIO()
        with mock.patch("sys.argv", ["new_posts.py", mode]), \
                mock.patch.object(new_posts, "read_record", return_value=snapshot), \
                mock.patch.object(new_posts, "read_feed", return_value=feed_xml), \
                mock.patch.object(new_posts, "write_record") as write_record, \
                contextlib.redirect_stdout(output):
            code = new_posts.main()
        return code, output.getvalue(), write_record

    def test_print_writes_nothing(self):
        snapshot = new_posts.RecordSnapshot("abc", {})
        code, _, write_record = self.run_main("print", snapshot, feed())
        self.assertEqual(code, 0)
        write_record.assert_not_called()

    def test_seeding_again_exits_with_an_error(self):
        snapshot = new_posts.RecordSnapshot("abc", {})
        one_post = feed(("A", "https://x/blogs/a/", "Thu, 29 Oct 2026 18:46:41 +0800", "x"))
        code, output, write_record = self.run_main("seed", snapshot, one_post)
        self.assertEqual(code, 1)
        self.assertIn("already holds a record", output)
        write_record.assert_not_called()

    def test_seeding_from_an_empty_feed_is_refused(self):
        snapshot = new_posts.RecordSnapshot(None, None)
        code, output, write_record = self.run_main("seed", snapshot, feed())
        self.assertEqual(code, 1)
        self.assertIn("no posts", output)
        write_record.assert_not_called()


class RecordTest(unittest.TestCase):
    """Runs the record functions against a real local git remote."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "--quiet", "--bare", self.remote], check=True)
        self.clone = self.make_clone(root / "clone")
        for key, value in (("user.name", "test"), ("user.email", "test@example.com")):
            subprocess.run(["git", "-C", self.clone, "config", key, value], check=True)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.clone)

    def make_clone(self, path):
        subprocess.run(
            ["git", "clone", "--quiet", self.remote, path],
            check=True, capture_output=True,
        )
        return path

    def seed(self, *paths):
        with contextlib.redirect_stdout(io.StringIO()):
            new_posts.seed_record(new_posts.read_record(), [post(p) for p in paths])

    def test_there_is_no_record_before_seeding(self):
        self.assertEqual(new_posts.read_record(), new_posts.RecordSnapshot(None, None))

    def test_a_seed_survives_into_a_fresh_clone(self):
        self.seed("/blogs/a/")
        os.chdir(self.make_clone(self.clone.parent / "fresh"))
        self.assertEqual(new_posts.read_record().entries, {"/blogs/a/": {"status": "seeded"}})

    def test_seeding_twice_is_refused(self):
        self.seed("/blogs/a/")
        with self.assertRaises(new_posts.Refused):
            self.seed("/blogs/b/")

    def test_a_later_write_keeps_the_branch_history(self):
        self.seed("/blogs/a/")
        new_posts.write_record({"/blogs/b/": {}}, new_posts.read_record(), "second")
        self.assertEqual(new_posts.read_record().entries, {"/blogs/b/": {}})
        log = new_posts.git("log", "--format=%s", new_posts.read_record().head)
        self.assertEqual(log.splitlines(), ["second", "seed 1 existing posts"])

    def test_a_branch_with_a_similar_name_is_not_the_record(self):
        new_posts.git("commit", "--allow-empty", "-m", "unrelated")
        new_posts.git("push", "--quiet", "origin", "HEAD:refs/heads/feat/newsletter-state")
        self.assertEqual(new_posts.read_record().entries, None)
        self.seed("/blogs/a/")
        self.assertEqual(set(new_posts.read_record().entries), {"/blogs/a/"})

    def test_a_run_working_from_a_stale_read_cannot_overwrite(self):
        self.seed("/blogs/a/")
        slower_run = new_posts.read_record()
        other_clone = self.make_clone(self.clone.parent / "other")
        os.chdir(other_clone)
        new_posts.write_record({"/blogs/b/": {}}, new_posts.read_record(), "faster run")
        os.chdir(self.clone)
        with self.assertRaises(subprocess.CalledProcessError):
            new_posts.write_record({"/blogs/c/": {}}, slower_run, "slower run")
        self.assertEqual(new_posts.read_record().entries, {"/blogs/b/": {}})

    def test_a_run_that_read_no_record_cannot_replace_one_written_since(self):
        unseeded = new_posts.read_record()
        self.seed("/blogs/a/")
        with self.assertRaises(subprocess.CalledProcessError):
            new_posts.write_record({"/blogs/b/": {}}, unseeded, "seed from a stale read")
        self.assertEqual(set(new_posts.read_record().entries), {"/blogs/a/"})

    def test_a_failure_to_reach_the_remote_is_not_a_missing_record(self):
        new_posts.git("remote", "set-url", "origin", "/nonexistent/remote.git")
        with self.assertRaises(subprocess.CalledProcessError):
            new_posts.read_record()


if __name__ == "__main__":
    unittest.main()
