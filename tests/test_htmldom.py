"""Tests for the pure-python HTML parser and CSS-lite selector engine."""
from __future__ import annotations

import unittest

from aninext.htmldom import (
    extract,
    extract_all,
    find,
    parse,
    render,
    select,
    table_to_dicts,
)

PAGE = """
<!doctype html>
<html><head><title>Test</title></head>
<body>
  <div id="wrap" class="main container" data-page="1">
    <ul class="cards">
      <li class="card item" data-id="1">
        <a href="/a/one" class="title">One <b>Piece</b></a>
        <img data-src="/img/1.jpg" alt="op poster">
        <span class="ep">12</span>
      </li>
      <li class="card item" data-id="2">
        <a href="/a/two" class="title">Frieren</a>
        <img src="/img/2.jpg" alt="">
        <span class="ep">28</span>
      </li>
      <li class="card third" data-id="3">
        <a href="//cdn.example.com/a/three" class="title">Bleach</a>
        <span class="ep">366</span>
      </li>
    </ul>
    <p>first<p>second
    <table>
      <tr><th>Name</th><th>Score</th></tr>
      <tr><td>One Piece</td><td>8.7</td></tr>
    </table>
    <div class="empty"></div>
    <video><source src="movie.mp4" type="video/mp4"><track label="English" srclang="en" src="en.vtt"></video>
  </div>
</body></html>
"""


class TestParser(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = parse(PAGE)

    def ids(self, selector: str) -> list[str]:
        return [n.get("data-id") for n in select(self.doc, selector)]

    def test_basic_selectors(self) -> None:
        self.assertEqual(len(select(self.doc, "div#wrap")), 1)
        self.assertEqual(len(select(self.doc, "div.main.container")), 1)
        self.assertEqual(len(select(self.doc, "ul.cards > li.card")), 3)
        self.assertEqual(len(select(self.doc, "ul.cards li")), 3)

    def test_nth_and_pseudo(self) -> None:
        self.assertEqual(self.ids("li:first-child"), ["1"])
        self.assertEqual(self.ids("li:last-child"), ["3"])
        self.assertEqual(self.ids("li:nth-child(2)"), ["2"])
        self.assertEqual(self.ids("li:nth-child(odd)"), ["1", "3"])
        self.assertEqual(self.ids("li:nth-child(2n+1)"), ["1", "3"])
        self.assertEqual(self.ids("li:not(.third)"), ["1", "2"])
        self.assertEqual(self.ids("li:has(span.ep)"), ["1", "2", "3"])
        self.assertEqual(self.ids('li:contains("frieren")'), ["2"])
        self.assertEqual(self.ids("li:matches(/Bleac/i)"), ["3"])
        self.assertEqual(len(select(self.doc, "div.empty:empty")), 1)

    def test_attribute_selectors(self) -> None:
        self.assertEqual(self.ids('li[data-id="2"]'), ["2"])
        self.assertEqual(self.ids('li[data-id^="1"]'), ["1"])
        self.assertEqual(self.ids('li[data-id$="3"]'), ["3"])
        self.assertEqual(self.ids('li[data-id*=""]'), ["1", "2", "3"])
        self.assertEqual(len(select(self.doc, "img[data-src]")), 1)

    def test_siblings(self) -> None:
        self.assertEqual(len(select(self.doc, "a + img")), 2)  # cards 1 and 2
        self.assertEqual(len(select(self.doc, "li.card + li.card")), 2)
        self.assertEqual([n.get("data-id") for n in select(self.doc, "li.card ~ li")], ["2", "3"])

    def test_selector_list(self) -> None:
        self.assertEqual(self.ids("li.card.third, li:first-child"), ["1", "3"])

    def test_text_and_attrs(self) -> None:
        link = find(self.doc, "li.card a.title")
        self.assertEqual(link.get("href"), "/a/one")
        self.assertEqual(link.text_content(), "One Piece")
        self.assertEqual(find(self.doc, "li.card img").get("src"), "/img/1.jpg")  # data-src fallback
        self.assertEqual(find(self.doc, "li.card img").get("alt"), "op poster")

    def test_extract_expressions(self) -> None:
        node = find(self.doc, "li.card")
        self.assertEqual(extract(node, "a.title @href"), "/a/one")
        self.assertEqual(extract(node, "a.title ::text"), "One Piece")
        self.assertEqual(extract(node, "a.title"), "One Piece")
        self.assertEqual(extract(node, "span.ep ::text"), "12")
        self.assertEqual(extract(node, "span.missing @href", "?"), "?")
        self.assertEqual(extract(node, "a.missing @href | a.title @href"), "/a/one")
        self.assertEqual(len(extract_all(self.doc, "li.card a.title")), 3)
        self.assertIn("<b>Piece</b>", extract(node, "a.title html"))

    def test_auto_close(self) -> None:
        self.assertEqual([n.text_content() for n in select(self.doc, "p")], ["first", "second"])
        self.assertEqual([n.get("data-id") for n in select(self.doc, "li")], ["1", "2", "3"])

    def test_table(self) -> None:
        rows = table_to_dicts(find(self.doc, "table"))
        self.assertEqual(rows, [{"Name": "One Piece", "Score": "8.7"}])

    def test_render_roundtrip(self) -> None:
        html = render(self.doc)
        again = parse(html)
        self.assertEqual(len(select(again, "li.card")), 3)
        self.assertEqual(extract(again, "a.title @href"), "/a/one")

    def test_malformed_html(self) -> None:
        doc = parse("<div><p>a<div>b</div><span>c</span></div><!-- comment -->")
        self.assertEqual(len(select(doc, "div")), 2)
        self.assertEqual(len(select(doc, "span")), 1)
        doc = parse("<ul><li>one<li>two<li>three</ul>")
        self.assertEqual([n.text_content() for n in select(doc, "li")], ["one", "two", "three"])

    def test_void_tags_do_not_nest(self) -> None:
        doc = parse("<p><img src='a.png'><br><span>x</span></p>")
        self.assertEqual(len(select(doc, "p span")), 1)
        self.assertEqual(extract(doc, "img @src"), "a.png")

    def test_entities(self) -> None:
        doc = parse("<p>Fish &amp; Chips &#039;quoted&#039;</p>")
        self.assertEqual(extract(doc, "p ::text"), "Fish & Chips 'quoted'")

    def test_case_insensitive_tags(self) -> None:
        doc = parse("<DIV CLASS='x'><SPAN>hi</SPAN></DIV>")
        self.assertEqual(extract(doc, "div.x span ::text"), "hi")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
