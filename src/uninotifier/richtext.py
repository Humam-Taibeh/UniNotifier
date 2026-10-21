"""
Portal HTML -> Telegram text.

Labels, section descriptions, activity descriptions, pages and forum posts
arrive as HTML. They are turned into readable legacy-Markdown text (headings
bold, list bullets, links kept clickable, tables flattened), and fingerprinted
so an edit by the instructor can be detected later.
"""

import hashlib
import re
from urllib.parse import unquote, urlparse

from lxml import html as lxml_html

from uninotifier.notifier import escape_md

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCKS = {"p", "div", "section", "article", "blockquote", "ul", "ol", "table", "tbody",
          "thead", "tr", "pre", "figure", "figcaption", "hr", "center", "dl", "dt", "dd"}
SKIP = {"script", "style", "noscript", "template", "button", "form", "svg"}
CELLS = {"td", "th"}
_HEADING_MARK = "\x00"  # marks a heading line while the text is assembled


def _parse(html: str):
    if not html or not html.strip():
        return None
    try:
        return lxml_html.fragment_fromstring(html, create_parent="div")
    except Exception:
        return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _walk(el, out: list[str], ordered: list[int]) -> None:
    tag = el.tag.lower() if isinstance(el.tag, str) else ""
    tail = escape_md(el.tail) if el.tail else ""

    if tag in SKIP or el.get("class", "").find("accesshide") >= 0:
        out.append(tail)
        return
    if tag == "br":
        out.append("\n" + tail)
        return
    if tag == "img":
        out.append(tail)
        return
    if tag in HEADINGS:
        text = _norm(el.text_content())
        if text:
            out.append(f"\n{_HEADING_MARK}*{escape_md(text)}*\n")
        out.append(tail)
        return
    if tag == "a":
        href, text = el.get("href", ""), _norm(el.text_content())
        # Moodle's auto-linking filter turns stray words or a lone "-" into
        # links to unrelated activities; only real link text stays a link.
        if href.startswith(("http://", "https://")) and len(re.findall(r"\w", text)) >= 2:
            out.append(f"[{escape_md(text)}]({href.replace(')', '%29')})" + tail)
            return

    if tag == "li":
        if ordered:
            ordered[-1] += 1
            out.append(f"\n{ordered[-1]}. ")
        else:
            out.append("\n• ")
    elif tag in BLOCKS:
        out.append("\n")
    if tag == "ol":
        ordered.append(0)

    if el.text:
        out.append(escape_md(el.text))
    for child in el:
        _walk(child, out, ordered)

    if tag == "ol":
        ordered.pop()
    if tag in CELLS:
        out.append(" · ")
    elif tag in BLOCKS or tag == "li":
        out.append("\n")
    out.append(tail)


def to_text(html: str) -> str:
    """Formatted Telegram (legacy Markdown) text of an HTML fragment."""
    root = _parse(html)
    if root is None:
        return ""
    out: list[str] = []
    _walk(root, out, [])
    # One line per paragraph / list item; a blank line before a heading that
    # starts a new part (a run of headings — some instructors write every
    # line as one — stays compact).
    lines, prev_heading = [], False
    for raw in "".join(out).split("\n"):
        line = re.sub(r"[ \t\xa0]+", " ", raw).strip().strip("·").strip()
        heading = line.startswith(_HEADING_MARK)
        line = line.removeprefix(_HEADING_MARK).strip()
        if not line:
            continue
        if heading and lines and not prev_heading:
            lines.append("")
        lines.append(line)
        prev_heading = heading
    return "\n".join(lines).strip()


def plain_text(html: str) -> str:
    root = _parse(html)
    return _norm(root.text_content()) if root is not None else ""


def image_paths(html: str) -> list[str]:
    root = _parse(html)
    if root is None:
        return []
    return sorted({unquote(urlparse(src).path) for src in root.xpath(".//img/@src") if src})


def links(html: str) -> list[str]:
    """Every absolute link in a fragment, plus bare URLs written as text."""
    root = _parse(html)
    if root is None:
        return []
    found = [h for h in root.xpath(".//a/@href") if h.startswith(("http://", "https://"))]
    found += re.findall(r"https?://[^\s<>\"')]+", root.text_content())
    return list(dict.fromkeys(found))


def is_meaningful(html: str) -> bool:
    """True when a fragment holds real content: at least a couple of letters
    or digits, or an image. A page whose body is just "." is not."""
    return len(re.findall(r"\w", plain_text(html))) >= 2 or bool(image_paths(html))


def digest(html: str) -> str:
    """Fingerprint of what a reader sees: the normalised text and the image
    files. Markup-only churn (attributes, whitespace, cache-busting query
    strings) doesn't change it."""
    basis = plain_text(html) + "\n" + "\n".join(image_paths(html))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def first_line(html: str, limit: int = 80) -> str:
    """A short title for an untitled fragment (labels have no name)."""
    root = _parse(html)
    if root is None:
        return ""
    for chunk in root.itertext():
        text = _norm(chunk)
        if len(re.findall(r"\w", text)) >= 2:
            return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return ""
