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


def post(path, age=timedelta(days=1), summary_html="<p>Hello.</p>", title="Title",
         summary_is_written=False):
    return new_posts.Post(
        path=path,
        title=title,
        link=f"https://mtmatt.csie.org{path}",
        published=NOW - age,
        summary_html=summary_html,
        summary_is_written=summary_is_written,
    )


class ParseFeedTest(unittest.TestCase):
    def test_notices_the_mark_on_a_written_summary(self):
        xml = (
            '<rss version="2.0"><channel><item><title>T</title>'
            "<link>https://mtmatt.csie.org/blogs/a/</link>"
            "<pubDate>Thu, 29 Oct 2026 18:46:41 +0800</pubDate><description>d</description>"
            '<summaryWritten xmlns="https://mtmatt.csie.org/feed">true</summaryWritten>'
            "</item></channel></rss>"
        )
        self.assertTrue(new_posts.parse_feed(xml)[0].summary_is_written)

    def test_reads_posts_and_decodes_the_escaped_summary(self):
        xml = feed((
            "多一雙手", "https://mtmatt.csie.org/blogs/hands/",
            "Thu, 29 Oct 2026 18:46:41 +0800", "&lt;p&gt;第一段&lt;/p&gt;",
        ))
        [parsed] = new_posts.parse_feed(xml)
        self.assertEqual(parsed.path, "/blogs/hands/")
        self.assertEqual(parsed.title, "多一雙手")
        self.assertEqual(parsed.summary_html, "<p>第一段</p>")
        self.assertFalse(parsed.summary_is_written)
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

    def test_refuses_a_feed_that_lists_a_post_twice(self):
        with self.assertRaises(new_posts.Refused):
            new_posts.split_new_posts([post("/blogs/a/"), post("/blogs/a/")], {}, NOW)

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

    def test_joins_chinese_sentences_across_paragraphs_without_a_space(self):
        summary_html = "<p>綠燈沒有亮。</p>\n<p>他說：「嗯。」</p>\n<p>再貼一次。</p>"
        self.assertEqual(new_posts.summarize(summary_html), "綠燈沒有亮。他說：「嗯。」再貼一次。")

    def test_limits_a_summary_taken_from_the_post(self):
        self.assertEqual(new_posts.summarize("<blockquote>一。二。三。四。</blockquote>"), "一。二。三。")

    def test_limits_a_summary_opening_with_something_other_than_a_block(self):
        for opening in ("<hr>", "<!-- note -->", '<img src="a.png">'):
            with self.subTest(opening=opening):
                summary_html = f"{opening}\n<p>一。二。三。四。</p>"
                self.assertEqual(new_posts.summarize(summary_html), "一。二。三。")

    def test_limits_a_summary_in_a_block_other_than_a_paragraph(self):
        self.assertEqual(new_posts.summarize("<section>一。二。三。四。</section>"), "一。二。三。")

    def test_keeps_spaces_in_text_that_is_not_chinese(self):
        for summary_html, expected in (
            ("<p>Café été.</p>", "Café été."),
            ("<p>“Yes.”</p>\n<p>“No.”</p>", "“Yes.” “No.”"),
        ):
            with self.subTest(summary_html=summary_html):
                self.assertEqual(new_posts.summarize(summary_html), expected)

    def test_block_tags_are_found_in_any_case(self):
        self.assertEqual(new_posts.summarize("<P>一。二。三。四。</P>"), "一。二。三。")

    def test_joins_chinese_paragraphs_however_they_end(self):
        self.assertEqual(new_posts.summarize("<p>他說……</p>\n<p>對。</p>"), "他說……對。")
        self.assertEqual(new_posts.summarize("<p>一。</p><p>二。</p>"), "一。二。")

    def test_tags_between_words_still_separate_english_ones(self):
        self.assertEqual(new_posts.summarize("<p>One</p><p>two.</p>"), "One two.")
        self.assertEqual(new_posts.summarize("<p>One.<hr>Two.</p>"), "One. Two.")

    def test_a_footnote_mark_stays_apart_from_its_word(self):
        summary_html = '<p>Text<sup><a href="#f">1</a></sup> more.</p>'
        self.assertEqual(new_posts.summarize(summary_html), "Text 1 more.")

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


class FakeKit:
    """Records Kit calls and answers them like the real API."""

    def __init__(self, stored_public=False, fail_on_create=False, stored_filter=None,
                 template_category="HTML", template_name="Monterey"):
        self.stored_public = stored_public
        self.fail_on_create = fail_on_create
        self.stored_filter = stored_filter
        self.template_category = template_category
        self.template_name = template_name
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/broadcasts":
            if self.fail_on_create:
                raise new_posts.KitError(500, "POST /broadcasts", "down")
            return {"broadcast": {
                "id": 99,
                "public": self.stored_public,
                "subscriber_filter": self.stored_filter or (body or {}).get("subscriber_filter"),
            }}
        if path == "/email_templates":
            return {"email_templates": [
                {"id": 1, "name": "Text only", "is_default": True, "category": "Classic"},
                {"id": 2, "name": self.template_name, "is_default": False,
                 "category": self.template_category},
            ]}
        if path == "/account":
            return {"account": {"primary_email_address": "owner@example.com"}}
        if path.startswith("/subscribers?"):
            return {"subscribers": [{"id": 7}]}
        if path == "/tags":
            return {"tag": {"id": 42}}
        if path.startswith("/tags/42/subscribers?"):
            return {"subscribers": [{"id": 7}]}
        if path == "/broadcasts/99/stats":
            return {"broadcast": {"stats": {"status": "completed", "recipients": 1}}}
        return None

    def bodies(self):
        return [c[2] for c in self.calls if c[:2] == ("POST", "/broadcasts")]


def no_sleep(_seconds):
    pass


class SummaryParagraphsTest(unittest.TestCase):
    def written(self, summary_html):
        return new_posts.summary_paragraphs(
            post("/blogs/a/", summary_html=summary_html, summary_is_written=True)
        )

    def test_reads_inline_markup_in_a_bare_summary(self):
        # Hugo renders a one-paragraph front matter summary with no <p>.
        summary_html = "我的<strong>摘要</strong>。二。<em>三</em>。四。<a href='x'>五</a>。"
        self.assertEqual(self.written(summary_html), ["我的摘要。二。三。四。五。"])

    def test_keeps_every_paragraph_whole(self):
        long_paragraph = f"一。二。三。四。五。{'長' * 300}"
        self.assertEqual(
            self.written(f"<p>{long_paragraph}</p>\n<p>第二段。</p>\n"),
            [long_paragraph, "第二段。"],
        )

    def test_lists_and_quotes_stay_apart_from_their_neighbours(self):
        summary_html = "<p>清單：</p>\n<ul>\n<li>甲項</li>\n<li>乙項</li>\n</ul>\n<blockquote>\n<p>引言。</p>\n</blockquote>\n<p>結尾。</p>"
        self.assertEqual(self.written(summary_html), ["清單：", "甲項", "乙項", "引言。", "結尾。"])

    def test_drops_empty_paragraphs(self):
        self.assertEqual(self.written("<p>一。</p>\n<p> </p>\n<p>二。</p>"), ["一。", "二。"])

    def test_joins_lines_wrapped_in_the_source(self):
        self.assertEqual(self.written("第一句，\n接著換行。\n第三句。"), ["第一句，接著換行。第三句。"])

    def test_keeps_a_typed_space(self):
        self.assertEqual(self.written("門打不開了 證還在"), ["門打不開了 證還在"])

    def test_limits_an_automatic_summary(self):
        automatic = post("/blogs/a/", summary_html="<p>一。二。三。四。</p>\n<p>五。</p>")
        self.assertEqual(new_posts.summary_paragraphs(automatic), ["一。二。三。"])

    def test_an_empty_automatic_summary_has_no_paragraphs(self):
        self.assertEqual(new_posts.summary_paragraphs(post("/blogs/a/", summary_html="")), [])


class SendNewPostsTest(unittest.TestCase):
    """Replaces only the record's git storage, keeping the claim order visible."""

    def setUp(self):
        self.writes = []

        def fake_write(entries, based_on, message):
            self.writes.append(entries)
            return new_posts.RecordSnapshot("next", entries)

        patcher = mock.patch.object(new_posts, "write_record", fake_write)
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, entries, posts, kit):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            snapshot = new_posts.RecordSnapshot("head", entries)
            new_posts.send_new_posts(snapshot, posts, kit, NOW)
        return output.getvalue()

    def test_claims_the_post_before_calling_kit(self):
        kit = FakeKit()
        statuses_when_called = []
        real_call = kit.__call__

        def spying_kit(method, path, body=None):
            if path == "/broadcasts":
                statuses_when_called.append(self.writes[-1]["/blogs/new/"]["status"])
            return real_call(method, path, body)

        self.send({}, [post("/blogs/new/")], spying_kit)
        self.assertEqual(statuses_when_called, [new_posts.SENDING])

    def test_records_the_broadcast_after_kit_accepts_it(self):
        self.send({}, [post("/blogs/new/")], FakeKit())
        self.assertEqual(
            self.writes[-1]["/blogs/new/"],
            {"status": new_posts.SCHEDULED, "broadcast_id": 99},
        )

    def test_broadcast_is_private_scheduled_and_for_everyone(self):
        kit = FakeKit()
        self.send({}, [post("/blogs/new/")], kit)
        [body] = kit.bodies()
        self.assertIs(body["public"], False)
        self.assertEqual(body["send_at"], (NOW + new_posts.SEND_DELAY).isoformat())
        self.assertNotIn("subscriber_filter", body)
        self.assertEqual(body["subject"], "Title")

    def test_a_failed_call_leaves_the_post_claimed_so_it_is_never_resent(self):
        kit = FakeKit(fail_on_create=True)
        with self.assertRaises(new_posts.KitError):
            self.send({}, [post("/blogs/new/")], kit)
        claimed = self.writes[-1]
        self.assertEqual(claimed["/blogs/new/"]["status"], new_posts.SENDING)

        retry = FakeKit()
        output = self.send(claimed, [post("/blogs/new/")], retry)
        self.assertEqual(retry.calls, [])
        self.assertIn("::warning::send was not confirmed", output)

    def test_a_claim_that_cannot_be_written_stops_before_kit(self):
        kit = FakeKit()
        failure = subprocess.CalledProcessError(1, "git push")
        with mock.patch.object(new_posts, "write_record", side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError):
                self.send({}, [post("/blogs/new/")], kit)
        self.assertEqual(kit.bodies(), [])

    def test_a_post_stored_as_public_is_deleted(self):
        kit = FakeKit(stored_public=True)
        with self.assertRaises(new_posts.UnsafeTarget):
            self.send({}, [post("/blogs/new/")], kit)
        self.assertIn(("DELETE", "/broadcasts/99", None), kit.calls)

    def test_scheduled_posts_are_not_sent_again(self):
        kit = FakeKit()
        entries = {"/blogs/a/": {"status": new_posts.SCHEDULED, "broadcast_id": 1}}
        self.send(entries, [post("/blogs/a/")], kit)
        self.assertEqual(kit.calls, [])

    def test_seeded_and_dismissed_posts_are_not_sent(self):
        kit = FakeKit()
        entries = {
            "/blogs/a/": {"status": new_posts.SEEDED},
            "/blogs/b/": {"status": new_posts.DISMISSED},
        }
        self.send(entries, [post("/blogs/a/"), post("/blogs/b/")], kit)
        self.assertEqual(kit.calls, [])

    def test_an_approved_old_post_is_sent(self):
        kit = FakeKit()
        old = post("/blogs/old/", age=new_posts.MAX_POST_AGE + timedelta(days=1))
        self.send({"/blogs/old/": {"status": new_posts.APPROVED}}, [old], kit)
        self.assertEqual(len(kit.bodies()), 1)

    def test_refuses_without_a_record(self):
        with self.assertRaises(new_posts.Refused):
            self.send(None, [post("/blogs/a/")], FakeKit())

    def test_sends_with_the_named_template(self):
        kit = FakeKit()
        self.send({}, [post("/blogs/new/")], kit)
        self.assertEqual([b["email_template_id"] for b in kit.bodies()], [2])

    def test_a_template_without_a_footer_stops_before_anything_is_claimed(self):
        kit = FakeKit(template_category=new_posts.STARTING_POINT_TEMPLATE)
        with self.assertRaises(new_posts.Refused):
            self.send({}, [post("/blogs/new/")], kit)
        self.assertEqual(self.writes, [])
        self.assertEqual(kit.bodies(), [])

    def test_prints_the_broadcast_id_even_if_recording_it_fails(self):
        kit = FakeKit()
        output = io.StringIO()
        failure = subprocess.CalledProcessError(1, "git push")
        with mock.patch.object(new_posts, "write_record",
                               side_effect=[new_posts.RecordSnapshot("n", {}), failure]), \
                contextlib.redirect_stdout(output), \
                self.assertRaises(subprocess.CalledProcessError):
            new_posts.send_new_posts(
                new_posts.RecordSnapshot("head", {}), [post("/blogs/new/")], kit, NOW
            )
        self.assertIn("scheduled broadcast 99", output.getvalue())

    def test_a_template_like_title_stops_before_anything_is_claimed(self):
        templated = new_posts.Post("/blogs/t/", "{{ x }}", "https://x/blogs/t/", NOW, "")
        kit = FakeKit()
        with self.assertRaises(new_posts.Refused):
            self.send({}, [templated], kit)
        self.assertEqual(self.writes, [])
        self.assertEqual(kit.bodies(), [])


class SendTestModeTest(unittest.TestCase):
    def run_test_mode(self, kit):
        with contextlib.redirect_stdout(io.StringIO()):
            new_posts.send_test([post("/blogs/a/")], kit, lambda: NOW, no_sleep)

    def test_tries_a_draft_then_mails_only_the_owner_tag(self):
        kit = FakeKit()
        self.run_test_mode(kit)
        draft, real = kit.bodies()
        self.assertIsNone(draft["send_at"])
        self.assertNotIn("subscriber_filter", draft)
        self.assertLess(kit.calls.index(("DELETE", "/broadcasts/99", None)),
                        [c[1] for c in kit.calls].index("/account"))
        self.assertEqual(real["subscriber_filter"][0]["all"], [{"type": "tag", "ids": [42]}])
        self.assertTrue(real["subject"].startswith("[測試]"))

    def test_the_draft_and_the_test_both_use_the_named_template(self):
        kit = FakeKit()
        self.run_test_mode(kit)
        self.assertEqual([b["email_template_id"] for b in kit.bodies()], [2, 2])

    def test_deletes_a_broadcast_stored_for_a_wider_audience(self):
        everyone = [{"all": [{"type": "all_subscribers"}], "any": None, "none": None}]
        kit = FakeKit(stored_filter=everyone)
        with self.assertRaises(new_posts.UnsafeTarget):
            self.run_test_mode(kit)
        deletes = [c for c in kit.calls if c[0] == "DELETE"]
        self.assertEqual(len(deletes), 2)
        self.assertNotIn(("GET", "/broadcasts/99/stats", None), kit.calls)

    def test_the_send_time_is_taken_after_the_tag_check(self):
        moments = iter([NOW, NOW + timedelta(minutes=5)])
        kit = FakeKit()
        with contextlib.redirect_stdout(io.StringIO()):
            new_posts.send_test([post("/blogs/a/")], kit, lambda: next(moments), no_sleep)
        expected = NOW + timedelta(minutes=5) + new_posts.TEST_SEND_DELAY
        self.assertEqual(kit.bodies()[1]["send_at"], expected.isoformat())

    def test_records_nothing(self):
        with mock.patch.object(new_posts, "write_record") as write_record:
            self.run_test_mode(FakeKit())
        write_record.assert_not_called()

    def test_a_template_without_a_footer_stops_before_any_broadcast(self):
        kit = FakeKit(template_category=new_posts.STARTING_POINT_TEMPLATE)
        with self.assertRaises(new_posts.Refused):
            self.run_test_mode(kit)
        self.assertEqual(kit.bodies(), [])

    def test_refuses_an_empty_feed(self):
        with self.assertRaises(new_posts.Refused):
            new_posts.send_test([], FakeKit(), lambda: NOW, no_sleep)


class ResolvePostTest(unittest.TestCase):
    def resolve(self, entries, path, status):
        with mock.patch.object(new_posts, "write_record") as write_record, \
                contextlib.redirect_stdout(io.StringIO()):
            snapshot = new_posts.RecordSnapshot("head", entries)
            new_posts.resolve_post(snapshot, [post("/blogs/a/")], path, status)
        return write_record.call_args.args[0]

    def test_approves_a_held_back_post(self):
        entries = self.resolve({}, "/blogs/a/", new_posts.APPROVED)
        self.assertEqual(entries["/blogs/a/"], {"status": new_posts.APPROVED})

    def test_resolves_an_unconfirmed_send(self):
        entries = self.resolve(
            {"/blogs/a/": {"status": new_posts.SENDING}}, "/blogs/a/", new_posts.DISMISSED
        )
        self.assertEqual(entries["/blogs/a/"], {"status": new_posts.DISMISSED})

    def test_refuses_a_post_that_was_already_handled(self):
        for status in (new_posts.SEEDED, new_posts.SCHEDULED, new_posts.DISMISSED):
            with self.subTest(status=status), self.assertRaises(new_posts.Refused):
                self.resolve({"/blogs/a/": {"status": status}}, "/blogs/a/", new_posts.APPROVED)

    def test_refuses_approving_a_path_outside_the_feed(self):
        with self.assertRaises(new_posts.Refused):
            self.resolve({}, "/blogs/typo/", new_posts.APPROVED)

    def test_refuses_dismissing_a_mistyped_path(self):
        with self.assertRaises(new_posts.Refused):
            self.resolve({}, "/blogs/a", new_posts.DISMISSED)

    def test_an_unconfirmed_post_that_left_the_feed_can_be_dismissed(self):
        entries = self.resolve(
            {"/blogs/gone/": {"status": new_posts.SENDING}}, "/blogs/gone/", new_posts.DISMISSED
        )
        self.assertEqual(entries["/blogs/gone/"], {"status": new_posts.DISMISSED})

    def test_a_mistaken_approval_can_be_withdrawn(self):
        entries = self.resolve(
            {"/blogs/a/": {"status": new_posts.APPROVED}}, "/blogs/a/", new_posts.DISMISSED
        )
        self.assertEqual(entries["/blogs/a/"], {"status": new_posts.DISMISSED})


class WrittenSummaryInEmailTest(unittest.TestCase):
    def test_each_paragraph_is_separate_and_cannot_inject_liquid(self):
        body = new_posts.email_html(post(
            "/blogs/a/", summary_html="<p>{{ a }}第一段。</p>\n<p>第二段。</p>",
            summary_is_written=True,
        ))
        self.assertEqual(body.count(new_posts.SUMMARY_STYLE), 2)
        self.assertNotIn("{{", body)

    def test_the_preview_joins_chinese_paragraphs_without_a_space(self):
        written = post("/blogs/a/", summary_html="<p>一。</p>\n<p>二。</p>", summary_is_written=True)
        self.assertEqual(new_posts.preview_text(written), "一。二。")

    def test_the_preview_is_capped_and_cannot_inject_liquid(self):
        long_summary = post("/blogs/a/", summary_html="{{ 長" * 100, summary_is_written=True)
        preview = new_posts.broadcast_body(long_summary, 2, NOW, None)["preview_text"]
        self.assertEqual(len(preview), new_posts.MAX_PREVIEW_LENGTH)
        self.assertNotIn("{", preview)


class FindEmailTemplateTest(unittest.TestCase):
    def find(self, kit):
        with contextlib.redirect_stdout(io.StringIO()):
            return new_posts.find_email_template(kit)

    def test_finds_the_template_by_name_rather_than_the_default(self):
        self.assertEqual(self.find(FakeKit()), 2)

    def test_refuses_when_the_template_is_missing(self):
        with self.assertRaises(new_posts.Refused):
            self.find(FakeKit(template_name="renamed"))

    def test_refuses_a_starting_point_template(self):
        with self.assertRaises(new_posts.Refused):
            self.find(FakeKit(template_category=new_posts.STARTING_POINT_TEMPLATE))


class EmailHtmlTest(unittest.TestCase):
    def test_has_the_cover_title_summary_and_link(self):
        body = new_posts.email_html(post("/blogs/a/"))
        self.assertIn('src="https://mtmatt.csie.org/Lac.jpg"', body)
        self.assertIn("Title</a></h1>", body)
        self.assertIn(">Hello.</p>", body)
        self.assertIn('href="https://mtmatt.csie.org/blogs/a/"', body)

    def test_omits_an_empty_summary(self):
        body = new_posts.email_html(post("/blogs/a/", summary_html="<h2>x</h2>"))
        self.assertNotIn(new_posts.SUMMARY_STYLE, body)

    def test_title_cannot_inject_markup_or_liquid(self):
        body = new_posts.email_html(post("/blogs/a/", title="<b>{x}</b>"))
        self.assertIn("&lt;b&gt;&#123;x}&lt;/b&gt;</a></h1>", body)

    def test_post_text_cannot_inject_liquid_into_the_preview(self):
        templated = post("/blogs/a/", summary_html="<p>{{ subscriber.email }} {% x %}.</p>")
        preview = new_posts.broadcast_body(templated, 2, NOW, None)["preview_text"]
        self.assertNotIn("{", preview)

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
    def run_main(self, *argv, snapshot, feed_xml="", env=None):
        output = io.StringIO()
        with mock.patch("sys.argv", ["new_posts.py", *argv]), \
                mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(new_posts, "read_record", return_value=snapshot), \
                mock.patch.object(new_posts, "read_feed", return_value=feed_xml or feed()), \
                mock.patch.object(new_posts, "write_record") as write_record, \
                mock.patch.object(new_posts, "send_new_posts") as send_new_posts, \
                contextlib.redirect_stdout(output):
            code = new_posts.main()
        return code, output.getvalue(), write_record, send_new_posts

    def test_print_writes_nothing(self):
        snapshot = new_posts.RecordSnapshot("abc", {})
        code, _, write_record, _ = self.run_main("print", snapshot=snapshot)
        self.assertEqual(code, 0)
        write_record.assert_not_called()

    def test_seeding_again_exits_with_an_error(self):
        snapshot = new_posts.RecordSnapshot("abc", {})
        one_post = feed(("A", "https://x/blogs/a/", "Thu, 29 Oct 2026 18:46:41 +0800", "x"))
        code, output, write_record, _ = self.run_main("seed", snapshot=snapshot, feed_xml=one_post)
        self.assertEqual(code, 1)
        self.assertIn("already holds a record", output)
        write_record.assert_not_called()

    def test_seeding_from_an_empty_feed_is_refused(self):
        snapshot = new_posts.RecordSnapshot(None, None)
        code, output, write_record, _ = self.run_main("seed", snapshot=snapshot)
        self.assertEqual(code, 1)
        self.assertIn("no posts", output)
        write_record.assert_not_called()

    def test_send_is_refused_unless_it_is_switched_on(self):
        snapshot = new_posts.RecordSnapshot("abc", {})
        for value in ("", "false", "1"):
            with self.subTest(value=value):
                code, output, _, send_new_posts = self.run_main(
                    "send", snapshot=snapshot, env={"SEND_ENABLED": value}
                )
                self.assertEqual(code, 1)
                self.assertIn("sending is off", output)
                send_new_posts.assert_not_called()

    def test_send_runs_when_switched_on(self):
        snapshot = new_posts.RecordSnapshot("abc", {})
        code, _, _, send_new_posts = self.run_main(
            "send", snapshot=snapshot, env={"SEND_ENABLED": "true"}
        )
        self.assertEqual(code, 0)
        send_new_posts.assert_called_once()

    def test_an_unexpected_error_shows_only_its_type(self):
        output = io.StringIO()
        with mock.patch("sys.argv", ["new_posts.py", "print"]), \
                mock.patch.object(new_posts, "read_record", side_effect=ValueError("secret")), \
                contextlib.redirect_stdout(output):
            code = new_posts.main()
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "::error::unexpected ValueError\n")


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

    def test_two_new_posts_in_one_run_are_each_mailed_once(self):
        self.seed("/blogs/old/")
        posts = [post("/blogs/old/"), post("/blogs/a/"), post("/blogs/b/")]
        kit = FakeKit()
        with contextlib.redirect_stdout(io.StringIO()):
            new_posts.send_new_posts(new_posts.read_record(), posts, kit, NOW)
            new_posts.send_new_posts(new_posts.read_record(), posts, kit, NOW)
        self.assertEqual(len(kit.bodies()), 2)
        entries = new_posts.read_record().entries
        self.assertEqual(entries["/blogs/old/"]["status"], new_posts.SEEDED)
        self.assertEqual(entries["/blogs/b/"]["status"], new_posts.SCHEDULED)

    def test_runs_making_identical_claims_get_different_commits(self):
        self.seed("/blogs/a/")
        snapshot = new_posts.read_record()
        entries = {"/blogs/b/": {"status": new_posts.SENDING}}
        same_second = {"GIT_AUTHOR_DATE": "2026-11-06T00:00:00Z",
                       "GIT_COMMITTER_DATE": "2026-11-06T00:00:00Z"}
        with mock.patch.dict(os.environ, {**same_second, "GITHUB_RUN_ID": "1"}):
            new_posts.write_record(entries, snapshot, "claim")
        with mock.patch.dict(os.environ, {**same_second, "GITHUB_RUN_ID": "2"}), \
                self.assertRaises(subprocess.CalledProcessError):
            new_posts.write_record(entries, snapshot, "claim")

    def test_a_failure_to_reach_the_remote_is_not_a_missing_record(self):
        new_posts.git("remote", "set-url", "origin", "/nonexistent/remote.git")
        with self.assertRaises(subprocess.CalledProcessError):
            new_posts.read_record()


if __name__ == "__main__":
    unittest.main()
