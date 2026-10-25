"""Static checks for the web UI files (no server, no browser)."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "calsync" / "web" / "static"
PAGES = ["index.html", "pair.html"]
VENDOR = STATIC / "vendor"
FONTS = [VENDOR / "fonts" / f"barlow-condensed-latin-{w}-normal.woff2" for w in (500, 600, 700)]
LICENSES = [VENDOR / "LICENSE-htm.txt", VENDOR / "LICENSE-preact.txt", VENDOR / "fonts" / "OFL.txt"]


class _Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.comments: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"src", "href"} and value:
                self.refs.append(value)

    def handle_comment(self, data: str) -> None:
        self.comments.append(data)


def _refs(page: str) -> list[str]:
    parser = _Refs()
    parser.feed((STATIC / page).read_text(encoding="utf-8"))
    return parser.refs


def _static_path(ref: str) -> Path:
    assert ref.startswith("/static/"), ref
    return STATIC / ref.removeprefix("/static/")


@pytest.mark.parametrize("page", PAGES)
def test_every_referenced_file_exists(page: str) -> None:
    refs = [r for r in _refs(page) if not r.startswith(("#", "data:"))]
    assert refs, "the page references no static files"
    for ref in refs:
        path = _static_path(ref)
        assert path.is_file(), f"{page} references missing {ref}"
        assert path.stat().st_size > 0, ref


def test_index_loads_the_app_module() -> None:
    assert "/static/app.js" in _refs("index.html")


def test_app_imports_resolve() -> None:
    for module in ("app.js", "i18n.js"):
        source = (STATIC / module).read_text(encoding="utf-8")
        for ref in re.findall(r'^import .*? from "([^"]+)";', source, flags=re.M):
            assert _static_path(ref).is_file(), f"{module} imports missing {ref}"


def test_vendored_module_is_javascript_with_the_hooks_we_use() -> None:
    source = (VENDOR / "htm-preact-standalone.mjs").read_text(encoding="utf-8")
    assert len(source) > 5000
    assert not source.lstrip().startswith("<")
    exports = re.search(r"export\{([^}]*)\}", source)
    assert exports, "no export list"
    names = {part.split(" as ")[-1].strip() for part in exports.group(1).split(",")}
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    imported = re.search(r'import \{([^}]*)\} from "/static/vendor/htm-preact-standalone\.mjs"', app)
    assert imported
    for name in (n.strip() for n in imported.group(1).split(",")):
        assert name in names, f"app.js imports {name}, which the module does not export"


@pytest.mark.parametrize("font", FONTS, ids=lambda p: p.name)
def test_fonts_are_woff2(font: Path) -> None:
    data = font.read_bytes()
    assert len(data) > 10_000
    assert data[:4] == b"wOF2"


@pytest.mark.parametrize("path", LICENSES, ids=lambda p: p.name)
def test_licenses_are_present(path: Path) -> None:
    assert path.stat().st_size > 500


@pytest.mark.parametrize("page", PAGES)
def test_pages_make_no_external_requests(page: str) -> None:
    text = (STATIC / page).read_text(encoding="utf-8")
    without_comments = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    assert not re.search(r"https?://", without_comments, flags=re.I), f"absolute URL in {page}"


def _zh_keys() -> set[str]:
    source = (STATIC / "i18n.js").read_text(encoding="utf-8")
    table = re.search(r"const zh = \{(.*?)\n\};", source, flags=re.S)
    assert table, "Chinese table not found"
    return set(re.findall(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', table.group(1), flags=re.M))


def test_every_t_key_has_a_chinese_string() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"', app))
    assert len(used) > 50
    missing = sorted(used - _zh_keys())
    assert not missing, f"keys without a Chinese string: {missing}"


def test_t_is_only_called_with_literal_keys() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    dynamic = re.findall(r'\bt\(\s*[^"\s)]', app)
    assert not dynamic, f"t() called with a non-literal key: {dynamic}"
