#!/usr/bin/env python3
"""
xcode_sdk_mapper.py  —  v9
==============================================================================================
Maps every Xcode version to its bundled iOS SDK version.

SOURCES  (ranked by authority, highest → lowest)
─────────────────────────────────────────────────────────────────────────────────────────────
  S1 · LOCAL  xcodebuild -showsdks
  S2 · Apple Developer Documentation JSON API
  S3 · Apple official developer support page (HTML)
  S4 · xcodereleases.com/data.json
  S5 · Apple library archive — Xcode 8–9 release notes
  S6 · Apple library archive — Xcode 4, 6, 7 chapter pages
  S7 · Wikipedia — "History of Xcode" article
  S8 · Wikipedia — "Xcode" main article

CHANGES FROM v8:
  • Removed github_xcodereleases: both GitHub mirror URLs return 404.
  • Removed xcode5 chapter from apple_archive_47: page parses to 0 versions.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── dependency check ────────────────────────────────────────────────────────
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit(
        "Missing dependencies. Install with:\n"
        "    pip install requests beautifulsoup4"
    )

# ── shared HTTP session ──────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

# ── source priority list (index 0 = highest authority) ──────────────────────
# apple_docs_json is now ranked #2 (above xcodereleases) because it comes
# directly from Apple's own documentation API.
SOURCE_NAMES = [
    "local_xcodebuild",       # S1  — locally installed Xcode (ground truth)
    "apple_docs_json",        # S2  — Apple docs JSON API (authoritative)
    "apple_support",          # S3  — Apple official HTML table
    "xcodereleases",          # S4  — community JSON API (xcodereleases.com)
    "apple_archive_9",        # S5  — Apple archive Xcode 8-9 (bullets+headings)
    "apple_archive_47",       # S6  — Apple archive Xcode 4, 6, 7 prose
    "wikipedia_history",      # S7  — Wikipedia "History of Xcode"
    "wikipedia_xcode",        # S8  — Wikipedia "Xcode" main article (strict prose)
]

# ── canonical URL(s) for each source — used in detailed CSV/JSON output
SOURCE_URLS: dict[str, str] = {
    "local_xcodebuild":     "local:xcodebuild -showsdks",
    "apple_docs_json":      "https://developer.apple.com/tutorials/data/documentation/xcode-release-notes/",
    "apple_support":        "https://developer.apple.com/support/xcode/",
    "xcodereleases":        "https://xcodereleases.com/data.json",
    "apple_archive_9":      "https://developer.apple.com/library/archive/releasenotes/DeveloperTools/RN-Xcode/Chapters/Introduction.html",
    "apple_archive_47":     "https://developer.apple.com/library/archive/documentation/Xcode/Conceptual/RN-Xcode-Archive/Chapters/",
    "wikipedia_history":    "https://en.wikipedia.org/wiki/History_of_Xcode",
    "wikipedia_xcode":      "https://en.wikipedia.org/wiki/Xcode",
}

# ── per-version URL overrides: source_name → {xcode_ver → actual_url}
_VERSION_URLS: dict[str, dict[str, str]] = {s: {} for s in SOURCE_NAMES}


# ════════════════════════════════════════════════════════════════════════════
# Version normalisation helpers
# ════════════════════════════════════════════════════════════════════════════

def _normalize_xcode_ver(ver: str) -> str:
    """
    Normalise an Xcode version string so bare majors become X.0.

    Examples:
      "9"     → "9.0"
      "9.0"   → "9.0"
      "9.4.1" → "9.4.1"
      "16"    → "16.0"
    """
    return ver if "." in ver else ver + ".0"


def _normalize_source_keys(data: dict[str, str]) -> dict[str, str]:
    """Return a copy of *data* with all keys passed through _normalize_xcode_ver."""
    out: dict[str, str] = {}
    for ver, sdk in data.items():
        normalised = _normalize_xcode_ver(ver)
        # first writer wins (higher-priority entries should come in first,
        # but here we just deduplicate within a single source dict)
        out.setdefault(normalised, sdk)
    return out


# ════════════════════════════════════════════════════════════════════════════
# Shared regex helpers
# ════════════════════════════════════════════════════════════════════════════

_IOS_SDK_PATTERNS = [
    re.compile(r"\biOS\s+(\d+(?:\.\d+)?)\s+SDK",                             re.I),
    re.compile(r"\biOS\s+SDK\s+(\d+(?:\.\d+)?)",                             re.I),
    re.compile(r"SDKs?\s+for\s+iOS\s+(?:/\s*iPadOS\s+)?(\d+(?:\.\d+)?)",    re.I),
    re.compile(r"includes?\s+the\s+iOS\s+(\d+(?:\.\d+)?)\s+SDK",            re.I),
    re.compile(r"includes?\s+SDKs?\s+for\s+iOS\s+(\d+(?:\.\d+)?)",          re.I),
    re.compile(r"adds?\s+support\s+for\s+(?:developing\s+apps?\s+with\s+)?iOS\s+(\d+(?:\.\d+)?)", re.I),
    re.compile(r"iOS\s+\((\d+(?:\.\d+)?)\)\s+SDK",                          re.I),
    re.compile(r"(?:shipped|released)\s+with\s+iOS\s+(\d+(?:\.\d+)?)",      re.I),
    re.compile(r"iOS\s+(\d+(?:\.\d+)?)\s+and\s+(?:OS\s+X|macOS)",          re.I),
]

_XCODE_VER_RE = re.compile(r"\bXcode\s+(\d+(?:\.\d+)*)", re.I)


def _normalize_sdk(ver: str) -> str:
    """Normalize an iOS SDK version to always have a minor component."""
    return ver if "." in ver else ver + ".0"


def _first_ios_sdk(text: str) -> Optional[str]:
    for pat in _IOS_SDK_PATTERNS:
        m = pat.search(text)
        if m:
            ver = _normalize_sdk(m.group(1))
            try:
                major = int(ver.split(".")[0])
                if 2 <= major <= 99:
                    return ver
            except ValueError:
                pass
    return None


def _parse_heading_prose(html: str, source_label: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, str] = {}
    headings = soup.find_all(re.compile(r"^h[2-4]$"))
    for heading in headings:
        heading_text = heading.get_text(" ", strip=True)
        xm = _XCODE_VER_RE.search(heading_text)
        if not xm:
            continue
        xcode_ver = _normalize_xcode_ver(xm.group(1))
        level_num = int(heading.name[1])
        stop_re = re.compile(r"^h[1-" + str(level_num) + "]$")
        parts: list[str] = []
        for sib in heading.find_next_siblings():
            if sib.name and stop_re.match(sib.name):
                break
            if hasattr(sib, "get_text"):
                parts.append(sib.get_text(" ", strip=True))
        ios_sdk = _first_ios_sdk(" ".join(parts))
        if ios_sdk and xcode_ver not in result:
            result[xcode_ver] = ios_sdk
    print(f"  [{source_label}] → {len(result)} versions", file=sys.stderr)
    return result


# ════════════════════════════════════════════════════════════════════════════
# S1 — Local xcodebuild -showsdks
# ════════════════════════════════════════════════════════════════════════════

def source_local_xcodebuild() -> dict[str, str]:
    print("  [local_xcodebuild] Querying local Xcode …", file=sys.stderr)
    try:
        ver_proc = subprocess.run(
            ["xcodebuild", "-version"],
            capture_output=True, text=True, timeout=30,
        )
        sdk_proc = subprocess.run(
            ["xcodebuild", "-showsdks"],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        print("  [local_xcodebuild] xcodebuild not found — skipping.", file=sys.stderr)
        return {}
    except subprocess.TimeoutExpired:
        print("  [local_xcodebuild] timed out — skipping.", file=sys.stderr)
        return {}

    xm = re.search(r"Xcode\s+(\d+(?:\.\d+)*)", ver_proc.stdout, re.I)
    if not xm:
        print("  [local_xcodebuild] Could not determine Xcode version.", file=sys.stderr)
        return {}
    xcode_ver = _normalize_xcode_ver(xm.group(1))

    im = re.search(r"iOS\s+(\d+(?:\.\d+)?)\s+-sdk\s+iphoneos", sdk_proc.stdout, re.I)
    if not im:
        print("  [local_xcodebuild] Could not find iphoneos SDK in showsdks.", file=sys.stderr)
        return {}
    ios_sdk = _normalize_sdk(im.group(1))

    _VERSION_URLS["local_xcodebuild"][xcode_ver] = "local:xcodebuild -showsdks"
    print(f"  [local_xcodebuild] Xcode {xcode_ver} → iOS SDK {ios_sdk}", file=sys.stderr)
    return {xcode_ver: ios_sdk}


# ════════════════════════════════════════════════════════════════════════════
# S4/S5 — xcodereleases.com JSON  (and GitHub mirror)
# ════════════════════════════════════════════════════════════════════════════

_XCODERELEASES_URL = "https://xcodereleases.com/data.json"


def _parse_xcodereleases_json(entries: list[dict], source_name: str, url: str) -> dict[str, str]:
    stable: dict[str, str] = {}
    non_stable: dict[str, str] = {}

    for entry in entries:
        ver_info = entry.get("version", {})
        xcode_ver: str = _normalize_xcode_ver(ver_info.get("number", ""))
        rel_info: dict = ver_info.get("release", {})
        ios_list: list = entry.get("sdks", {}).get("iOS", [])
        if not xcode_ver or not ios_list:
            continue
        ios_sdk_raw: str = ios_list[0].get("number", "")
        if not ios_sdk_raw:
            continue
        ios_sdk = _normalize_sdk(ios_sdk_raw)
        is_stable = bool(rel_info.get("release")) and not (
            rel_info.get("beta") or rel_info.get("rc")
        )
        target = stable if is_stable else non_stable
        target.setdefault(xcode_ver, ios_sdk)

    merged = {**non_stable, **stable}
    for xver in merged:
        _VERSION_URLS[source_name][xver] = url
    return merged


def source_xcodereleases() -> dict[str, str]:
    print("  [xcodereleases] Fetching JSON …", file=sys.stderr)
    try:
        resp = SESSION.get(_XCODERELEASES_URL, timeout=30)
        resp.raise_for_status()
        entries: list[dict] = resp.json()
    except Exception as exc:
        print(f"  [xcodereleases] FAILED: {exc}", file=sys.stderr)
        return {}

    result = _parse_xcodereleases_json(entries, "xcodereleases", _XCODERELEASES_URL)
    print(f"  [xcodereleases] → {len(result)} versions", file=sys.stderr)
    return result


# ════════════════════════════════════════════════════════════════════════════
# S3 — Apple developer support page (HTML table)
# ════════════════════════════════════════════════════════════════════════════

_APPLE_SUPPORT_URL = "https://developer.apple.com/support/xcode/"


def source_apple_support() -> dict[str, str]:
    print("  [apple_support] Fetching HTML table …", file=sys.stderr)
    try:
        resp = SESSION.get(_APPLE_SUPPORT_URL, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  [apple_support] FAILED: {exc}", file=sys.stderr)
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    result: dict[str, str] = {}

    for table in soup.find_all("table"):
        ver_col: Optional[int] = None
        sdk_col: Optional[int] = None

        header_row = table.find("tr")
        if not header_row:
            continue
        header_cells = header_row.find_all(["th", "td"])
        for idx, cell in enumerate(header_cells):
            text = cell.get_text(" ", strip=True).lower()
            if ver_col is None and "xcode" in text and "version" in text:
                ver_col = idx
            if sdk_col is None and "sdk" in text:
                sdk_col = idx

        if ver_col is None or sdk_col is None:
            ver_col, sdk_col = 0, 2

        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) <= max(ver_col, sdk_col):
                continue

            xcode_text = cells[ver_col].get_text(" ", strip=True)
            xm = re.search(r"Xcode\s+(\d+(?:\.\d+)*)", xcode_text, re.I)
            if not xm:
                continue
            xcode_ver = _normalize_xcode_ver(xm.group(1))

            sdk_text = cells[sdk_col].get_text(" ", strip=True)
            im = re.search(r"iOS\s+(\d+(?:\.\d+)?)", sdk_text)
            if im:
                ios_sdk = _normalize_sdk(im.group(1))
                if result.setdefault(xcode_ver, ios_sdk) == ios_sdk:
                    _VERSION_URLS["apple_support"][xcode_ver] = _APPLE_SUPPORT_URL

    print(f"  [apple_support] → {len(result)} versions", file=sys.stderr)
    return result


# ════════════════════════════════════════════════════════════════════════════
# S2 — Apple Developer Documentation JSON API  (highest non-local authority)
# ════════════════════════════════════════════════════════════════════════════

_APPLE_DOCS_JSON_BASE = (
    "https://developer.apple.com/tutorials/data/documentation/"
    "xcode-release-notes/xcode-{slug}-release-notes.json"
)

_APPLE_DOCS_MAJOR_RANGE = list(range(8, 17)) + list(range(26, 36))
_APPLE_DOCS_MINOR_RANGE = list(range(0, 8))
_APPLE_DOCS_PATCH_RANGE = list(range(0, 5))


def _make_slug(major: int, minor: int, patch: int = 0) -> tuple[str, str]:
    if minor == 0 and patch == 0:
        return str(major) + ".0", str(major)   # normalised ver → "16.0"
    if patch == 0:
        return f"{major}.{minor}", f"{major}_{minor}"
    return f"{major}.{minor}.{patch}", f"{major}_{minor}_{patch}"


def source_apple_docs_json() -> dict[str, str]:
    print("  [apple_docs_json] Fetching release-note JSONs …", file=sys.stderr)
    result: dict[str, str] = {}

    def fetch_one(major: int, minor: int, patch: int) -> tuple[str, Optional[str], str]:
        xcode_ver, slug = _make_slug(major, minor, patch)
        url = _APPLE_DOCS_JSON_BASE.format(slug=slug)
        try:
            resp = SESSION.get(url, timeout=20)
            if resp.status_code == 404:
                return xcode_ver, None, url
            if resp.status_code != 200:
                return xcode_ver, None, url
            data = resp.json()
        except Exception:
            return xcode_ver, None, url

        full_text = json.dumps(data)
        ios_sdk = _first_ios_sdk(full_text)
        return xcode_ver, ios_sdk, url

    tasks = [
        (major, minor, patch)
        for major in _APPLE_DOCS_MAJOR_RANGE
        for minor in _APPLE_DOCS_MINOR_RANGE
        for patch in _APPLE_DOCS_PATCH_RANGE
        if not (minor == 0 and patch > 0)
    ]

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(fetch_one, maj, min_, pat): (maj, min_, pat)
                   for maj, min_, pat in tasks}
        for fut in as_completed(futures):
            xcode_ver, ios_sdk, fetched_url = fut.result()
            if ios_sdk:
                if result.setdefault(xcode_ver, ios_sdk) == ios_sdk:
                    _VERSION_URLS["apple_docs_json"].setdefault(xcode_ver, fetched_url)

    print(f"  [apple_docs_json] → {len(result)} versions", file=sys.stderr)
    return result


# ════════════════════════════════════════════════════════════════════════════
# S6 — Apple archive Xcode 8–9 release notes
# ════════════════════════════════════════════════════════════════════════════

_APPLE_ARCHIVE_XCODE9_URL = (
    "https://developer.apple.com/library/archive/releasenotes/"
    "DeveloperTools/RN-Xcode/Chapters/Introduction.html"
)


def source_apple_archive_9() -> dict[str, str]:
    print("  [apple_archive_9] Fetching HTML …", file=sys.stderr)
    try:
        resp = SESSION.get(_APPLE_ARCHIVE_XCODE9_URL, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  [apple_archive_9] FAILED: {exc}", file=sys.stderr)
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    result: dict[str, str] = {}

    all_headings = soup.find_all(re.compile(r"^h[1-6]$"))
    for heading in all_headings:
        heading_text = heading.get_text(" ", strip=True)
        xm = _XCODE_VER_RE.search(heading_text)
        if not xm:
            continue
        raw_ver = xm.group(1)

        # Skip bare major headings (no dot) — their intro prose describes the
        # latest patch SDK, not the X.0 SDK.
        if "." not in raw_ver:
            continue

        xcode_ver = _normalize_xcode_ver(raw_ver)

        parts: list[str] = []
        for sib in heading.find_next_siblings():
            if sib.name and re.match(r"^h[1-6]$", sib.name):
                break
            if hasattr(sib, "get_text"):
                parts.append(sib.get_text(" ", strip=True))

        ios_sdk = _first_ios_sdk(" ".join(parts))
        if ios_sdk and xcode_ver not in result:
            result[xcode_ver] = ios_sdk
            _VERSION_URLS["apple_archive_9"][xcode_ver] = _APPLE_ARCHIVE_XCODE9_URL

    print(
        f"  [apple_archive_9] → {len(result)} versions "
        "(versioned headings only, no propagation)",
        file=sys.stderr,
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
# S7 — Apple archive Xcode 4–7 chapter pages
# ════════════════════════════════════════════════════════════════════════════

_APPLE_ARCHIVE_CHAPTER_BASE = (
    "https://developer.apple.com/library/archive/documentation/"
    "Xcode/Conceptual/RN-Xcode-Archive/Chapters/"
)
_APPLE_ARCHIVE_CHAPTERS = {
    "xcode4": "xc4_release_notes.html",
    # xcode5 removed: page parses to 0 versions
    "xcode6": "xc6_release_notes.html",
    "xcode7": "xc7_release_notes.html",
}


def source_apple_archive_47() -> dict[str, str]:
    combined: dict[str, str] = {}

    def fetch_chapter(label: str, filename: str) -> dict[str, str]:
        url = _APPLE_ARCHIVE_CHAPTER_BASE + filename
        print(f"  [apple_archive_47/{label}] Fetching {url} …", file=sys.stderr)
        try:
            resp = SESSION.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            print(f"  [apple_archive_47/{label}] FAILED: {exc}", file=sys.stderr)
            return {}
        chapter_result = _parse_heading_prose(resp.text, f"apple_archive_47/{label}")
        for xver in chapter_result:
            _VERSION_URLS["apple_archive_47"][xver] = url
        return chapter_result

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(fetch_chapter, lbl, fn)
            for lbl, fn in _APPLE_ARCHIVE_CHAPTERS.items()
        ]
        for fut in as_completed(futures):
            for ver, sdk in fut.result().items():
                combined.setdefault(ver, sdk)

    print(f"  [apple_archive_47] → {len(combined)} total", file=sys.stderr)
    return combined


# ════════════════════════════════════════════════════════════════════════════
# S8 — Wikipedia "History of Xcode"
# ════════════════════════════════════════════════════════════════════════════

_WIKIPEDIA_HISTORY_URL = "https://en.wikipedia.org/wiki/History_of_Xcode"


def _parse_wikipedia_prose(html: str, source_label: str, source_name: str, url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["nav", "table", "sup", "style", "script"]):
        tag.decompose()
    full_text = soup.get_text(" ", strip=True)
    sentences = re.split(r"\.(?:\s|\n)+", full_text)

    result: dict[str, str] = {}
    for sentence in sentences:
        xcode_versions = [_normalize_xcode_ver(v) for v in _XCODE_VER_RE.findall(sentence)]
        if not xcode_versions:
            continue
        ios_sdk = _first_ios_sdk(sentence)
        if not ios_sdk:
            co = re.search(r"\biOS\s+(\d+(?:\.\d+)?)\s+and\s+Xcode", sentence, re.I)
            if co:
                ios_sdk = _normalize_sdk(co.group(1))
        if ios_sdk:
            for xv in xcode_versions:
                result.setdefault(xv, ios_sdk)

    for xver in result:
        _VERSION_URLS[source_name][xver] = url

    print(f"  [{source_label}] → {len(result)} versions", file=sys.stderr)
    return result


def source_wikipedia_history() -> dict[str, str]:
    print("  [wikipedia_history] Fetching article …", file=sys.stderr)
    try:
        resp = SESSION.get(_WIKIPEDIA_HISTORY_URL, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  [wikipedia_history] FAILED: {exc}", file=sys.stderr)
        return {}
    return _parse_wikipedia_prose(
        resp.text, "wikipedia_history", "wikipedia_history", _WIKIPEDIA_HISTORY_URL
    )


# ════════════════════════════════════════════════════════════════════════════
# S9 — Wikipedia "Xcode" main article
# ════════════════════════════════════════════════════════════════════════════

_WIKIPEDIA_XCODE_URL = "https://en.wikipedia.org/wiki/Xcode"


def source_wikipedia_xcode() -> dict[str, str]:
    print("  [wikipedia_xcode] Fetching article …", file=sys.stderr)
    try:
        resp = SESSION.get(_WIKIPEDIA_XCODE_URL, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  [wikipedia_xcode] FAILED: {exc}", file=sys.stderr)
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup.find_all(["nav", "table", "sup", "style", "script"]):
        tag.decompose()

    full_text = soup.get_text(" ", strip=True)
    full_text = re.sub(r"\[\d+\]", "", full_text)
    sentences = re.split(r"\.(?:\s|\n)+", full_text)
    result: dict[str, str] = {}

    _strict_sdk_patterns = [
        re.compile(r"\biOS\s+(\d+(?:\.\d+)?)\s+SDK",                           re.I),
        re.compile(r"\biOS\s+SDK\s+(\d+(?:\.\d+)?)",                           re.I),
        re.compile(r"SDKs?\s+for\s+iOS\s+(?:/\s*iPadOS\s+)?(\d+(?:\.\d+)?)",  re.I),
        re.compile(r"includes?\s+the\s+iOS\s+(\d+(?:\.\d+)?)\s+SDK",          re.I),
        re.compile(r"includes?\s+SDKs?\s+for\s+iOS\s+(\d+(?:\.\d+)?)",        re.I),
        re.compile(r"shipped\s+with\s+iOS\s+(\d+(?:\.\d+)?)",                  re.I),
        re.compile(r"released\s+with\s+iOS\s+(\d+(?:\.\d+)?)",                 re.I),
        re.compile(r"\biOS\s+(\d+(?:\.\d+)?)\s+and\s+Xcode",                  re.I),
    ]

    def _strict_ios_sdk(text: str) -> Optional[str]:
        for pat in _strict_sdk_patterns:
            m = pat.search(text)
            if m:
                ver = _normalize_sdk(m.group(1))
                try:
                    if 2.0 <= float(ver.split(".")[0]) <= 26.0:
                        return ver
                except ValueError:
                    pass
        return None

    for sentence in sentences:
        xcode_versions = [_normalize_xcode_ver(v) for v in _XCODE_VER_RE.findall(sentence)]
        if not xcode_versions:
            continue
        ios_sdk = _strict_ios_sdk(sentence)
        if ios_sdk:
            for xv in xcode_versions:
                result.setdefault(xv, ios_sdk)

    for xver in result:
        _VERSION_URLS["wikipedia_xcode"][xver] = _WIKIPEDIA_XCODE_URL

    print(f"  [wikipedia_xcode] → {len(result)} versions (strict prose only)", file=sys.stderr)
    return result


# ════════════════════════════════════════════════════════════════════════════
# xcodes CLI
# ════════════════════════════════════════════════════════════════════════════

def get_xcodes_versions() -> list[str]:
    try:
        proc = subprocess.run(
            ["xcodes", "list", "--data-source", "apple"],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        print(
            "  WARNING: `xcodes` not found — version list from CLI skipped.\n"
            "  Install: brew install robotsandpencils/made/xcodes",
            file=sys.stderr,
        )
        return []
    except subprocess.TimeoutExpired:
        print("  WARNING: `xcodes list` timed out.", file=sys.stderr)
        return []

    if proc.returncode != 0:
        print(f"  WARNING: xcodes exited {proc.returncode}", file=sys.stderr)

    raw = proc.stdout + proc.stderr
    found = re.findall(r"(?<![.\d])(\d+\.\d+(?:\.\d+)?)(?![.\d])", raw)
    seen: set[str] = set()
    unique: list[str] = []
    for v in found:
        normalised = _normalize_xcode_ver(v)
        if normalised not in seen:
            seen.add(normalised)
            unique.append(normalised)
    return unique


# ════════════════════════════════════════════════════════════════════════════
# Cross-reference engine
# ════════════════════════════════════════════════════════════════════════════

def cross_reference(
    xcode_ver: str,
    all_sources: dict[str, dict[str, str]],
) -> dict:
    """
    Cross-reference one Xcode version across all sources.

    Conflict resolution: SOURCE_NAMES is ordered highest-to-lowest priority.
    apple_docs_json (S2) now outranks xcodereleases (S4).
    """
    per_source: dict[str, Optional[str]] = {}
    found_values: dict[str, str] = {}

    for src_name, src_data in all_sources.items():
        sdk = src_data.get(xcode_ver)
        # Fallback: X.Y.Z → X.Y  (patch → minor, but NOT minor → major)
        if sdk is None:
            parts = xcode_ver.split(".")
            if len(parts) == 3:
                sdk = src_data.get(".".join(parts[:2]))
        per_source[src_name] = sdk
        if sdk is not None:
            found_values[src_name] = sdk

    unique_values = set(found_values.values())

    def _url_for(src_name: str) -> str:
        ver_urls = _VERSION_URLS.get(src_name, {})
        url = ver_urls.get(xcode_ver)
        if url:
            return url
        parts = xcode_ver.split(".")
        if len(parts) == 3:
            url = ver_urls.get(".".join(parts[:2]))
            if url:
                return url
        return SOURCE_URLS.get(src_name, "")

    if not unique_values:
        return {
            "xcode": xcode_ver, "ios_sdk": None,
            "status": "not_found", "chosen_from": None,
            "agreement": 0.0,
            "sources": {
                s: {"value": "—", "url": _url_for(s)}
                for s in all_sources
            },
        }

    if len(unique_values) == 1:
        ios_sdk = next(iter(unique_values))
        n = len(found_values)
        status = "consensus" if n > 1 else "single_source"
        chosen_from = next(
            (s for s in SOURCE_NAMES if found_values.get(s) == ios_sdk),
            next(iter(found_values)),
        )
        agreement = 1.0
    else:
        status = "conflict"
        # Pick the highest-priority source that has a value
        chosen_from = next(
            (s for s in SOURCE_NAMES if s in found_values),
            next(iter(found_values)),
        )
        ios_sdk = found_values[chosen_from]
        n_agree = sum(1 for v in found_values.values() if v == ios_sdk)
        agreement = n_agree / len(found_values) if found_values else 0.0

    return {
        "xcode": xcode_ver,
        "ios_sdk": ios_sdk,
        "status": status,
        "chosen_from": chosen_from,
        "agreement": round(agreement, 2),
        "sources": {
            s: {
                "value": found_values.get(s) or "—",
                "url":   _url_for(s),
            }
            for s in all_sources
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# Output helpers
# ════════════════════════════════════════════════════════════════════════════

def write_csv_simple(results: list[dict], path: str) -> None:
    """Default output: two columns only — xcode_version, ios_sdk."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["xcode_version", "ios_sdk"])
        writer.writeheader()
        for row in results:
            writer.writerow({
                "xcode_version": row["xcode"],
                "ios_sdk":       row["ios_sdk"] or "",
            })
    print(f"\n  ✓ CSV saved → {path}", file=sys.stderr)


def write_json_simple(results: list[dict], path: str, metadata: dict) -> None:
    """Default output: array of {xcode_version, ios_sdk} objects."""
    output = {
        "metadata": {**metadata, "source_urls": SOURCE_URLS},
        "results": [
            {"xcode_version": r["xcode"], "ios_sdk": r["ios_sdk"] or ""}
            for r in results
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2)
    print(f"  ✓ JSON saved → {path}", file=sys.stderr)


def write_csv_detailed(results: list[dict], path: str, source_names: list[str]) -> None:
    """Detailed output: all per-source columns + agreement info."""
    fieldnames = [
        "xcode_version",
        "ios_sdk",
        "status",
        "chosen_from",
        "agreement_pct",
    ]
    for s in source_names:
        fieldnames.append(f"src_{s}")
        fieldnames.append(f"src_{s}_url")

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            flat = {
                "xcode_version":  row["xcode"],
                "ios_sdk":        row["ios_sdk"] or "",
                "status":         row["status"],
                "chosen_from":    row["chosen_from"] or "",
                "agreement_pct":  f"{row['agreement'] * 100:.0f}%",
            }
            for s in source_names:
                entry = row["sources"].get(s, {})
                if isinstance(entry, dict):
                    flat[f"src_{s}"]     = entry.get("value", "—")
                    flat[f"src_{s}_url"] = entry.get("url", "")
                else:
                    flat[f"src_{s}"]     = entry
                    flat[f"src_{s}_url"] = SOURCE_URLS.get(s, "")
            writer.writerow(flat)
    print(f"\n  ✓ CSV saved → {path}", file=sys.stderr)


def write_json_detailed(results: list[dict], path: str, metadata: dict) -> None:
    """Detailed output: full per-source breakdown."""
    output = {"metadata": {**metadata, "source_urls": SOURCE_URLS}, "results": results}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2)
    print(f"  ✓ JSON saved → {path}", file=sys.stderr)


def print_table(results: list[dict], source_names: list[str]) -> None:
    icons = {
        "consensus":     "✓",
        "single_source": "~",
        "conflict":      "⚠",
        "not_found":     "✗",
    }
    short_names = [s[:10] for s in source_names]
    hdr = (
        f"  {'Xcode':>10}  {'iOS SDK':>8}  St  Agr  "
        + "  ".join(f"{n:<10}" for n in short_names)
    )
    sep = "─" * len(hdr)
    print(f"\n{sep}", file=sys.stderr)
    print(hdr, file=sys.stderr)
    print(sep, file=sys.stderr)
    for row in results:
        sdk = row["ios_sdk"] or "—"
        st  = icons.get(row["status"], "?")
        agr = f"{row['agreement']*100:.0f}%"
        srcs = "  ".join(
            f"{(row['sources'].get(s) or {}).get('value', '—'):<10}"
            for s in source_names
        )
        url_hint = ""
        if row["status"] == "conflict" and row.get("chosen_from"):
            winning_url = (
                _VERSION_URLS.get(row["chosen_from"], {}).get(row["xcode"])
                or SOURCE_URLS.get(row["chosen_from"], "")
            )
            url_hint = f"  ← {winning_url}"
        print(
            f"  Xcode {row['xcode']:>8}  {sdk:>8}  {st}   {agr:>3}  {srcs}{url_hint}",
            file=sys.stderr,
        )
    print(sep, file=sys.stderr)


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Map Xcode versions → iOS SDK (8 sources, auto CSV+JSON).\n\n"
            "Default output: xcode_version + ios_sdk only (clean).\n"
            "Use --detailed for the full per-source breakdown."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--csv",  metavar="PATH", default="xcode_ios_sdk_map.csv",
                    help="CSV output path (default: xcode_ios_sdk_map.csv)")
    ap.add_argument("--json", metavar="PATH", default="xcode_ios_sdk_map.json",
                    help="JSON output path (default: xcode_ios_sdk_map.json)")
    ap.add_argument("--detailed", action="store_true",
                    help="Include per-source breakdown in CSV/JSON output")
    ap.add_argument("--table", action="store_true",
                    help="Print human-readable table to stderr")
    ap.add_argument("--conflicts-only", action="store_true",
                    help="Restrict output files to conflicting/missing rows only")
    ap.add_argument("--skip-xcodes", action="store_true",
                    help="Skip the xcodes CLI version enumeration")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc).isoformat()
    print(f"\n{'═'*70}", file=sys.stderr)
    print(f"  xcode_sdk_mapper  v9  —  {ts}", file=sys.stderr)
    mode_label = "DETAILED" if args.detailed else "SIMPLE (xcode_version + ios_sdk)"
    print(f"  Output mode: {mode_label}", file=sys.stderr)
    print(f"{'═'*70}\n", file=sys.stderr)

    # ── Step 1: Enumerate Xcode versions via xcodes CLI ──────────────────
    if args.skip_xcodes:
        print("Step 1: Skipping xcodes CLI (--skip-xcodes).\n", file=sys.stderr)
        xcodes_versions: list[str] = []
    else:
        print("Step 1: Querying xcodes CLI …", file=sys.stderr)
        xcodes_versions = get_xcodes_versions()
        print(f"  Found {len(xcodes_versions)} versions from xcodes.\n", file=sys.stderr)

    # ── Step 2: Fetch all sources in parallel ─────────────────────────────
    print("Step 2: Fetching all sources in parallel …\n", file=sys.stderr)

    raw_sources: dict[str, dict[str, str]] = {}

    task_map = {
        "local_xcodebuild":     source_local_xcodebuild,
        "apple_docs_json":      source_apple_docs_json,
        "apple_support":        source_apple_support,
        "xcodereleases":        source_xcodereleases,
        "apple_archive_9":      source_apple_archive_9,
        "apple_archive_47":     source_apple_archive_47,
        "wikipedia_history":    source_wikipedia_history,
        "wikipedia_xcode":      source_wikipedia_xcode,
    }

    def _run(name: str, fn) -> tuple[str, dict]:
        return name, fn()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_run, n, f): n for n, f in task_map.items()}
        for fut in as_completed(futures):
            name, data = fut.result()
            # Normalise all version keys so "9" and "9.0" are treated as one
            raw_sources[name] = _normalize_source_keys(data)

    ordered_sources: dict[str, dict[str, str]] = {
        s: raw_sources.get(s, {}) for s in SOURCE_NAMES
    }

    # ── Step 3: Build universe of Xcode versions ─────────────────────────
    print("\nStep 3: Building version universe …", file=sys.stderr)
    all_versions: set[str] = set(xcodes_versions)
    for src_data in ordered_sources.values():
        all_versions.update(src_data.keys())

    if not all_versions:
        sys.exit("ERROR: No Xcode versions found from any source. Check network.")

    def ver_key(v: str) -> list[int]:
        try:
            return [int(x) for x in v.split(".")]
        except ValueError:
            return [0]

    sorted_versions = sorted(all_versions, key=ver_key, reverse=True)
    print(f"  {len(sorted_versions)} unique Xcode versions to resolve.", file=sys.stderr)

    # ── Step 4: Cross-reference ───────────────────────────────────────────
    print("\nStep 4: Cross-referencing all sources …", file=sys.stderr)
    results: list[dict] = []
    n_consensus = n_conflict = n_single = n_missing = 0

    for ver in sorted_versions:
        entry = cross_reference(ver, ordered_sources)
        results.append(entry)
        s = entry["status"]
        if s == "consensus":       n_consensus += 1
        elif s == "conflict":      n_conflict  += 1
        elif s == "single_source": n_single    += 1
        else:                      n_missing   += 1

    total = len(results)
    coverage = (total - n_missing) / total * 100 if total else 0
    print(
        f"  {n_consensus} consensus  |  {n_single} single-source  |  "
        f"{n_conflict} conflicts  |  {n_missing} not-found  (total {total})\n"
        f"  Coverage: {coverage:.1f}%",
        file=sys.stderr,
    )
    if n_conflict:
        print(
            f"  ⚠  {n_conflict} conflict(s) — re-run with --detailed to inspect "
            "per-source values.",
            file=sys.stderr,
        )

    if args.table:
        print_table(results, SOURCE_NAMES)

    output_rows = (
        [r for r in results if r["status"] in ("conflict", "not_found")]
        if args.conflicts_only
        else results
    )

    # ── Step 5: Write outputs ─────────────────────────────────────────────
    print("\nStep 5: Writing outputs …", file=sys.stderr)

    metadata = {
        "generated_at": ts,
        "total_versions": total,
        "consensus": n_consensus,
        "single_source": n_single,
        "conflicts": n_conflict,
        "not_found": n_missing,
        "coverage_pct": round(coverage, 1),
        "sources": SOURCE_NAMES,
        "detailed": args.detailed,
    }

    if args.detailed:
        write_csv_detailed(output_rows, args.csv, SOURCE_NAMES)
        write_json_detailed(output_rows, args.json, metadata)
    else:
        write_csv_simple(output_rows, args.csv)
        write_json_simple(output_rows, args.json, metadata)

    print(f"\n{'═'*70}", file=sys.stderr)
    print(f"  Done. {len(output_rows)} rows written.", file=sys.stderr)
    if n_conflict and not args.detailed:
        print(
            f"  Tip: re-run with --detailed to inspect {n_conflict} conflict(s) "
            "and per-source values.",
            file=sys.stderr,
        )
    print(f"{'═'*70}\n", file=sys.stderr)


if __name__ == "__main__":
    main()