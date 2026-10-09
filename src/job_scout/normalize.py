"""Convert untrusted HTML to readable plain text without executing it."""

import re
from html import unescape
from html.parser import HTMLParser


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "template"}:
            self.hidden.append(tag)
        if not self.hidden and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "ul", "ol"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "ul", "ol"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def html_to_text(value: str, *, encoded: bool = False) -> str:
    # Greenhouse may encode markup twice; decoding is bounded.
    if encoded:
        for _ in range(2):
            value = unescape(value)
    parser = _TextParser()
    parser.feed(value)
    parser.close()
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)
