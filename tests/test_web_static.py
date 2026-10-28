"""Static checks for the web UI files: the files themselves, and how the web app serves them (no
browser; the browser tests are in ``tests/browser``)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest

from calsync.web import auth
from calsync.web.app import create_web_app
from tests.harness import config_for

STATIC = Path(__file__).resolve().parent.parent / "src" / "calsync" / "web" / "static"
PAGES = ["index.html", "pair.html"]
VENDOR = STATIC / "vendor"
VENDOR_MODULE = "/static/vendor/htm-preact-standalone.mjs"
FONT = VENDOR / "fonts" / "instrument-sans.woff2"
LICENSES = [
    VENDOR / "LICENSE-htm.txt",
    VENDOR / "LICENSE-preact.txt",
    VENDOR / "fonts" / "OFL-instrument-sans.txt",
]
# Our own modules (everything but the vendored files), found rather than listed: a module added
# by a later change is checked without editing this file.
MODULES = sorted(p for p in STATIC.rglob("*.js") if VENDOR not in p.parents)
STYLESHEETS = sorted(STATIC.glob("*.css"))
IMPORT = re.compile(r'^\s*import\s[^;]*?\sfrom\s+"([^"]+)";', flags=re.M | re.S)


def _rel(path: Path) -> str:
    return path.relative_to(STATIC).as_posix()


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class _Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"src", "href"} and value:
                self.refs.append(value)


def _refs(page: str) -> list[str]:
    parser = _Refs()
    parser.feed(_source(STATIC / page))
    return parser.refs


def _static_path(ref: str) -> Path:
    assert ref.startswith("/static/"), ref
    return STATIC / ref.removeprefix("/static/")


def test_the_modules_are_found() -> None:
    names = {_rel(p) for p in MODULES}
    assert {"app.js", "api.js", "i18n.js", "format.js", "icons.js", "theme.js"} <= names


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


@pytest.mark.parametrize("module", MODULES, ids=_rel)
def test_module_imports_resolve(module: Path) -> None:
    for ref in IMPORT.findall(_source(module)):
        assert _static_path(ref).is_file(), f"{_rel(module)} imports missing {ref}"


def test_the_vendored_module_exports_what_the_modules_import() -> None:
    source = _source(VENDOR / "htm-preact-standalone.mjs")
    assert len(source) > 5000
    assert not source.lstrip().startswith("<")
    exports = re.search(r"export\{([^}]*)\}", source)
    assert exports, "no export list"
    names = {part.split(" as ")[-1].strip() for part in exports.group(1).split(",")}
    imported: set[str] = set()
    pattern = re.compile(r'import\s*\{([^}]*)\}\s*from\s*"' + re.escape(VENDOR_MODULE) + '"', flags=re.S)
    for module in MODULES:
        for group in pattern.findall(_source(module)):
            imported |= {n.strip() for n in group.split(",") if n.strip()}
    assert {"html", "render"} <= imported
    assert imported <= names, f"not exported by the vendored module: {sorted(imported - names)}"


def test_the_font_is_woff2() -> None:
    data = FONT.read_bytes()
    assert len(data) > 10_000
    assert data[:4] == b"wOF2"


@pytest.mark.parametrize("path", LICENSES, ids=lambda p: p.name)
def test_licenses_are_present(path: Path) -> None:
    assert path.stat().st_size > 500


def test_the_font_licence_names_instrument_sans() -> None:
    text = _source(VENDOR / "fonts" / "OFL-instrument-sans.txt")
    assert "Instrument Sans" in text and "SIL Open Font License" in text


def test_the_stylesheet_loads_the_vendored_font() -> None:
    css = _source(STATIC / "styles.css")
    faces = re.findall(r"@font-face\s*\{[^}]*\}", css)
    assert faces and all('"Instrument Sans"' in face for face in faces)
    for url in re.findall(r'url\("?([^")]+)"?\)', css):
        assert _static_path(url).is_file(), url


def test_nothing_refers_to_the_old_fonts() -> None:
    for path in [*MODULES, *STYLESHEETS, *(STATIC / p for p in PAGES)]:
        assert "barlow" not in _source(path).lower(), _rel(path)
    assert not list((VENDOR / "fonts").glob("barlow*"))


@pytest.mark.parametrize("path", [*(STATIC / p for p in PAGES), *STYLESHEETS, *MODULES], ids=_rel)
def test_nothing_is_loaded_from_another_origin(path: Path) -> None:
    text = re.sub(r"<!--.*?-->", "", _source(path), flags=re.S)
    assert not re.search(r"https?://", text, flags=re.I), f"absolute URL in {_rel(path)}"


def _zh_keys() -> set[str]:
    table = re.search(r"const zh = \{(.*?)\n\};", _source(STATIC / "i18n.js"), flags=re.S)
    assert table, "Chinese table not found"
    return set(re.findall(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', table.group(1), flags=re.M))


def _t_calls() -> Iterator[tuple[Path, str]]:
    for module in MODULES:
        for key in re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"', _source(module)):
            yield module, key


def test_every_t_key_has_a_chinese_string() -> None:
    used = {key for _, key in _t_calls()}
    assert len(used) > 20
    missing = sorted(used - _zh_keys())
    assert not missing, f"keys without a Chinese string: {missing}"


def test_t_is_only_called_with_literal_keys() -> None:
    for module in MODULES:
        dynamic = re.findall(r'(?<!function )\bt\(\s*[^"\s)]', _source(module))
        assert not dynamic, f"{_rel(module)} calls t() with a non-literal key: {dynamic}"


@pytest.mark.parametrize("module", MODULES, ids=_rel)
def test_modules_never_write_style_strings(module: Path) -> None:
    """The CSP (``default-src 'self'``) refuses inline style strings: geometry goes through SVG
    attributes or Preact style objects (CSSOM), never ``style="…"`` or ``setAttribute("style")``."""
    source = _source(module)
    assert not re.search(r"""\bstyle\s*=\s*["'`]""", source), "a style attribute string"
    assert not re.search(r"""\bstyle\s*=\s*\$\{\s*[`"']""", source), "a style string in a template"
    assert not re.search(r"""setAttribute\(\s*["']style["']""", source), 'setAttribute("style")'
    assert ".cssText" not in source and "innerHTML" not in source


class _Inline(HTMLParser):
    """Inline scripts and styles, which the pages' CSP (``default-src 'self'``) would block."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []
        self.head: list[str] = []
        self._in_head = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "head":
            self._in_head = True
        if self._in_head and tag in {"script", "link"}:
            self.head.append(values.get("src") or values.get("href") or "")
        if tag == "script" and not values.get("src"):
            self.found.append("<script> without src")
        if tag == "style":
            self.found.append("<style>")
        if "style" in values:
            self.found.append(f"style= on <{tag}>")
        self.found += [f"{name}= on <{tag}>" for name in values if name.startswith("on")]

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._in_head = False


def _inline(text: str) -> _Inline:
    parser = _Inline()
    parser.feed(text)
    return parser


@pytest.mark.parametrize("page", PAGES)
def test_pages_have_no_inline_script_or_style(page: str) -> None:
    parser = _inline(_source(STATIC / page))
    assert parser.found == []
    # The theme before first paint: a classic script, first in <head>, before any stylesheet.
    assert parser.head[0] == "/static/theme.js"


# ----- as served --------------------------------------------------------------------------------


@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_web_app(
        config_provider=lambda: config_for([]), state_dir=tmp_path, daemon=None, listen_host="127.0.0.1"
    )
    key = auth.load_or_create_key(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766") as client:
        client.cookies.set(auth.COOKIE, auth.session_token(key))
        yield client


@pytest.mark.anyio
async def test_served_pages_have_no_inline_script_or_style(served: httpx.AsyncClient) -> None:
    paired = await served.get("/")
    served.cookies.clear()
    unpaired = await served.get("/")
    assert '<script type="module" src="/static/app.js">' in paired.text
    assert 'action="/pair"' in unpaired.text and "app.js" not in unpaired.text
    for response in (paired, unpaired):
        assert response.status_code == 200
        assert response.headers["content-security-policy"].startswith("default-src 'self'")
        assert _inline(response.text).found == []


JAVASCRIPT = {"text/javascript", "application/javascript"}


@pytest.mark.anyio
@pytest.mark.parametrize("module", [*MODULES, VENDOR / "htm-preact-standalone.mjs"], ids=_rel)
async def test_every_module_is_served_as_javascript(served: httpx.AsyncClient, module: Path) -> None:
    response = await served.get(f"/static/{_rel(module)}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0].strip() in JAVASCRIPT
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("styles.css", "text/css"),
        ("pair.css", "text/css"),
        ("favicon.svg", "image/svg+xml"),
        ("vendor/fonts/instrument-sans.woff2", "font/woff2"),
    ],
)
async def test_other_assets_are_served_with_their_type(
    served: httpx.AsyncClient, path: str, kind: str
) -> None:
    response = await served.get(f"/static/{path}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0].strip() == kind


def test_the_wires_take_their_colours_from_tokens() -> None:
    """Every colour on the map's wires is a token (light and dark set it once each)."""
    css = _source(STATIC / "styles.css")
    for rule in re.findall(r"^[^{}]*\.wires[^{}]*\{[^}]*\}", css, flags=re.M):
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rule), rule
