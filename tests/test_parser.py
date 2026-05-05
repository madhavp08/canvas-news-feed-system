"""Tests for parser.py — News Feed extraction and normalization."""

import hashlib

import pytest

from parser import ParseError, canonical_line_plain, normalize_text, parse_news_feed

# ---------------------------------------------------------------------------
# Representative HTML that mimics the real Canvas course page structure:
#   - Surrounding noise content (Lectures, Projects sections)
#   - A "News feed" heading
#   - Two dated entry blocks with rich formatting inside list items
# ---------------------------------------------------------------------------

SAMPLE_HTML = """\
<html>
<head><base href="https://umd.instructure.com/"/></head>
<body>
<div id="course-homepage">

  <h2>Lectures</h2>
  <ul>
    <li><a href="/lectures/1">Lecture 1: Intro</a></li>
    <li><a href="/lectures/2">Lecture 2: Pointers</a></li>
  </ul>

  <h2>Projects</h2>
  <ul>
    <li>Project #8: Due Friday</li>
  </ul>

  <h2>News feed</h2>

  <h3>Saturday, April 25</h3>
  <ol style="list-style-type: decimal;">
    <li>
      There are some students who still have to take the
      <b>exam</b>. Please check your email for details.
    </li>
    <li>
      About <b>Project #9</b>: Read the
      <a href="/projects/9">project requirements</a> section
      carefully before starting.
    </li>
  </ol>

  <h3>Friday, April 24</h3>
  <ol style="list-style-type: decimal;">
    <li>
      Remember that this weekend we have
      <b>no office hours</b>.
    </li>
    <li>
      I was asked to pass information along about the
      <a href="https://example.com">career fair</a> next week.
    </li>
    <li>
      There are some students who still have to take the exam.
    </li>
  </ol>

  <h2>Exams</h2>
  <p>Midterm 1: March 10</p>

</div>
</body>
</html>
"""

# Minimal HTML with just the News Feed section, no noise.
MINIMAL_HTML = """\
<html><body>
<h2>News Feed</h2>
<h3>Monday, April 21</h3>
<ol><li>Single announcement item.</li></ol>
</body></html>
"""

NO_NEWS_FEED_HTML = """\
<html><body>
<h2>Lectures</h2>
<p>Nothing here.</p>
</body></html>
"""

EMPTY_NEWS_FEED_HTML = """\
<html><body>
<h2>News feed</h2>
<p>Welcome to the course.</p>
</body></html>
"""


class TestNormalizeText:
    def test_collapses_whitespace(self):
        assert normalize_text("  hello   world  ") == "hello world"

    def test_collapses_newlines_and_tabs(self):
        assert normalize_text("line1\n\t  line2\n") == "line1 line2"

    def test_empty_string(self):
        assert normalize_text("") == ""

    def test_already_clean(self):
        assert normalize_text("no extra spaces") == "no extra spaces"


class TestParseNewsFeed:
    def test_entry_count(self):
        entries = parse_news_feed(SAMPLE_HTML)
        assert len(entries) == 2

    def test_newest_first_ordering(self):
        entries = parse_news_feed(SAMPLE_HTML)
        assert entries[0]["date_raw"] == "Saturday, April 25"
        assert entries[1]["date_raw"] == "Friday, April 24"

    def test_date_normalization(self):
        entries = parse_news_feed(SAMPLE_HTML)
        assert entries[0]["date_normalized"] == "saturday, april 25"
        assert entries[1]["date_normalized"] == "friday, april 24"

    def test_item_count_per_entry(self):
        entries = parse_news_feed(SAMPLE_HTML)
        assert len(entries[0]["items"]) == 2
        assert len(entries[1]["items"]) == 3

    def test_resolves_canvas_relative_anchor_with_base_tag(self):
        entries = parse_news_feed(SAMPLE_HTML)
        bullets = entries[0]["items"][1]
        links = [s for s in bullets if s["type"] == "link"]
        assert len(links) == 1
        assert links[0]["href"] == "https://umd.instructure.com/projects/9"
        assert "project requirements" in links[0]["label"].lower()

    def test_external_https_anchor_becomes_link(self):
        entries = parse_news_feed(SAMPLE_HTML)
        bullets = entries[1]["items"][1]
        links = [s for s in bullets if s["type"] == "link"]
        assert len(links) == 1
        assert links[0]["href"].startswith("https://example.com")
        assert "career fair" in links[0]["label"].lower()

    def test_no_raw_tags_in_segments(self):
        entries = parse_news_feed(SAMPLE_HTML)
        flat = repr(entries[0]["items"])
        assert "<b>" not in flat
        assert "<a " not in flat

    def test_normalizes_whitespace_in_plaintext_lines(self):
        entries = parse_news_feed(SAMPLE_HTML)
        for entry in entries:
            for line in entry["items"]:
                plain = canonical_line_plain(line)
                assert plain == plain.strip()

    def test_content_text_equals_canonical_join_of_lines(self):
        entries = parse_news_feed(SAMPLE_HTML)
        for entry in entries:
            expected = "\n".join(
                canonical_line_plain(line) for line in entry["items"]
            )
            assert entry["content_text"] == expected

    def test_content_hash_is_sha256(self):
        entries = parse_news_feed(SAMPLE_HTML)
        first = entries[0]
        expected = hashlib.sha256(first["content_text"].encode("utf-8")).hexdigest()
        assert first["content_hash"] == expected
        assert len(first["content_hash"]) == 64

    def test_ignores_noise_sections(self):
        """Lectures, Projects, and Exams content must not appear in entries."""
        entries = parse_news_feed(SAMPLE_HTML)
        all_text = " ".join(e["content_text"] for e in entries)
        assert "Lecture 1" not in all_text
        assert "Midterm" not in all_text
        assert "Project #8" not in all_text

    def test_stops_at_next_top_level_section(self):
        """Parser must stop before the Exams h2 and not crash."""
        entries = parse_news_feed(SAMPLE_HTML)
        dates = [e["date_raw"] for e in entries]
        assert "Exams" not in " ".join(dates)

    def test_minimal_single_entry(self):
        entries = parse_news_feed(MINIMAL_HTML)
        assert len(entries) == 1
        assert entries[0]["date_raw"] == "Monday, April 21"
        assert entries[0]["items"] == [
            [{"type": "text", "text": "Single announcement item."}]
        ]

    def test_case_insensitive_heading_match(self):
        """'News Feed' with capital F should still be found."""
        entries = parse_news_feed(MINIMAL_HTML)
        assert len(entries) == 1

    def test_spaces_around_anchor_preserved_in_segments(self):
        """Boundary spaces next to <a> must survive stripping (email clients)."""
        html = """\
<html><head><base href="https://school.instructure.com/"/></head><body>
<h2>News feed</h2>
<h3>Monday, May 5</h3>
<ol><li>Before <a href="/files/1.pdf">the doc</a> after.</li></ol>
</body></html>
"""
        entries = parse_news_feed(html)
        line = entries[0]["items"][0]
        rendered = "".join(
            s["text"]
            if s["type"] == "text"
            else s["label"]
            for s in line
        )
        assert rendered == "Before the doc after."
        typ = [s["type"] for s in line]
        assert typ == ["text", "link", "text"]
        assert line[0]["text"].endswith(" ")
        assert line[2]["text"].startswith(" ")


class TestParserErrors:
    def test_no_news_feed_section(self):
        with pytest.raises(ParseError, match="Could not find"):
            parse_news_feed(NO_NEWS_FEED_HTML)

    def test_news_feed_with_no_entries(self):
        with pytest.raises(ParseError, match="zero date/list entry pairs"):
            parse_news_feed(EMPTY_NEWS_FEED_HTML)


class TestFormattingResilience:
    """Verify that formatting-only differences do not change the parsed output."""

    BOLD_VARIANT = """\
    <html><body>
    <h2>News feed</h2>
    <h3>Sunday, April 27</h3>
    <ol><li>Check your <b>grades</b> online.</li></ol>
    </body></html>
    """

    PLAIN_VARIANT = """\
    <html><body>
    <h2>News feed</h2>
    <h3>Sunday, April 27</h3>
    <ol><li>Check your grades online.</li></ol>
    </body></html>
    """

    def test_bold_vs_plain_produces_same_normalized_text(self):
        bold_entries = parse_news_feed(self.BOLD_VARIANT)
        plain_entries = parse_news_feed(self.PLAIN_VARIANT)
        assert bold_entries[0]["content_text"] == plain_entries[0]["content_text"]
        assert bold_entries[0]["content_hash"] == plain_entries[0]["content_hash"]
