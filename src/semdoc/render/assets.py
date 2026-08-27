"""Vendoring the Mermaid renderer for local output.

Published Artifacts render Mermaid natively, so the fragment output needs no JavaScript.
A local HTML file has no such host, so the standalone output ships Mermaid itself.

The bundle is fetched once into `~/.semdoc/vendor` and reused across projects and runs.
The `dist/mermaid.min.js` build is an IIFE that assigns a `mermaid` global, which is what
lets the generated file work over `file://` — the ESM build would need module loading that
browsers block on local files.
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil

import httpx

# Pinned rather than floating on @11, so a rebuild months from now produces the same
# output. Bump deliberately.
MERMAID_VERSION = "11.17.2"
MERMAID_URL = f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js"

CACHE_DIR = pathlib.Path.home() / ".semdoc" / "vendor"

# Sentinel proving we got the IIFE build with the global assignment, not an ESM bundle or
# a CDN error page.
_EXPECTED_MARKER = 'globalThis["mermaid"]'

_MIN_PLAUSIBLE_BYTES = 500_000


class AssetError(RuntimeError):
    pass


def _cache_paths() -> tuple[pathlib.Path, pathlib.Path]:
    js = CACHE_DIR / f"mermaid-{MERMAID_VERSION}.min.js"
    return js, js.with_suffix(".sha256")


def _verify(text: str) -> None:
    if len(text) < _MIN_PLAUSIBLE_BYTES or _EXPECTED_MARKER not in text:
        raise AssetError(
            f"Downloaded Mermaid bundle does not look like the expected IIFE build "
            f"({len(text):,} bytes). Refusing to embed it."
        )


def fetch_mermaid(*, refresh: bool = False) -> str:
    """Return the Mermaid bundle source, downloading and caching it on first use.

    The recorded digest guards the cache against truncated or partially written files. It
    is an integrity check on our own cache, not a supply-chain guarantee — the pinned
    version in the URL is what fixes the code we embed.
    """
    js_path, sha_path = _cache_paths()

    if js_path.exists() and not refresh:
        text = js_path.read_text(encoding="utf-8")
        if sha_path.exists():
            expected = sha_path.read_text(encoding="utf-8").strip()
            actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if expected != actual:
                raise AssetError(
                    f"Cached Mermaid bundle at {js_path} is corrupt "
                    f"(digest mismatch). Delete it and re-run."
                )
        _verify(text)
        return text

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        response = httpx.get(MERMAID_URL, follow_redirects=True, timeout=180.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise AssetError(
            f"Could not download Mermaid from {MERMAID_URL}: {exc}\n"
            f"Diagrams need this once; afterwards it is cached in {CACHE_DIR}."
        ) from exc

    text = response.text
    _verify(text)

    js_path.write_text(text, encoding="utf-8")
    sha_path.write_text(hashlib.sha256(text.encode("utf-8")).hexdigest(), encoding="utf-8")
    return text


def install_mermaid(out_dir: pathlib.Path, *, refresh: bool = False) -> pathlib.Path:
    """Copy the Mermaid bundle into `out_dir/vendor` and return its path.

    Kept as a sibling file rather than inlined by default: the two standalone guides would
    otherwise carry ~3.5 MB each of identical JavaScript.
    """
    source = fetch_mermaid(refresh=refresh)
    vendor_dir = out_dir / "vendor"
    vendor_dir.mkdir(parents=True, exist_ok=True)
    target = vendor_dir / "mermaid.min.js"

    # Skip the rewrite when the content already matches, so repeated renders do not churn
    # a 3.5 MB file.
    if not target.exists() or target.read_text(encoding="utf-8") != source:
        target.write_text(source, encoding="utf-8")

    return target


def clear_cache() -> None:
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
