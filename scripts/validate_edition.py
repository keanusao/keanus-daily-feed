"""Quality checks for data/edition.json.

Usage:
  python scripts/validate_edition.py            # structure + freshness
  python scripts/validate_edition.py --links    # also check every source link

Writes a Markdown report to check-report.md and exits 1 if any check fails.
Broken links (404/410/unreachable on three tries over ~10 minutes) fail the check; sites that
block bots (401/403/429/5xx) are listed as "couldn't verify" but don't fail it.
"""
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTIONS = {"politics", "world", "economy", "stocks", "engineering", "science", "ai", "health", "local"}
LEANS = {"L", "LL", "C", "LR", "R"}
STALE_HOURS = 15.5
RETRY_WAITS = (120, 480)  # seconds before each re-check of a link that looked broken
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def check_structure(d):
    errors, warnings = [], []
    for key in ("updatedAt", "label", "summary", "stories", "highlights"):
        if key not in d:
            errors.append(f"Missing top-level field `{key}`.")
    stories = d.get("stories") or []
    if len(stories) < 8:
        errors.append(f"Only {len(stories)} stories (expected at least 8).")
    ids = set()
    for s in stories:
        sid = s.get("id", "?")
        if sid in ids:
            errors.append(f"Duplicate story id `{sid}`.")
        ids.add(sid)
        if s.get("section") not in SECTIONS:
            errors.append(f"Story `{sid}` has unknown section `{s.get('section')}`.")
        if not s.get("title"):
            errors.append(f"Story `{sid}` has no title.")
        if len(s.get("facts") or []) < 3:
            errors.append(f"Story `{sid}` has fewer than 3 facts.")
        if len(s.get("story") or []) < 2:
            warnings.append(f"Story `{sid}` has fewer than 2 paragraphs.")
        src = s.get("src") or []
        if len(src) < 2:
            errors.append(f"Story `{sid}` has fewer than 2 sources.")
        for x in src:
            if not (isinstance(x, list) and len(x) == 3 and x[1] in LEANS and str(x[2]).startswith("http")):
                errors.append(f"Story `{sid}` has a malformed source entry: {x!r}.")
    for h in d.get("highlights") or []:
        if h.get("story") not in ids:
            errors.append(f"Highlight points to missing story `{h.get('story')}`.")
    for a in d.get("alerts") or []:
        if a.get("story") and a["story"] not in ids:
            warnings.append(f"Alert points to missing story `{a['story']}`.")
    if str(d.get("summary", "")).count("**") % 2:
        warnings.append("Summary has an unmatched ** highlight marker.")
    present = {s.get("section") for s in stories}
    for need in ("politics", "engineering", "science", "ai"):
        if need not in present:
            warnings.append(f"No `{need}` stories in this edition.")
    return errors, warnings


def check_fresh(d):
    try:
        t = datetime.fromisoformat(str(d["updatedAt"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return ["`updatedAt` is missing or not a valid timestamp."]
    hours = (datetime.now(timezone.utc) - t).total_seconds() / 3600
    pt = datetime.now(ZoneInfo("America/Los_Angeles"))
    # Weekends have a single morning edition, so allow a full day between editions.
    limit = 25.5 if pt.weekday() in (5, 6) or (pt.weekday() == 0 and pt.hour < 7) else STALE_HOURS
    if hours > limit:
        return [f"The latest edition is {hours:.1f} hours old (expected a new one within {limit:.0f} hours)."]
    return []


def probe(url):
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, headers=UA, method=method)
            with urllib.request.urlopen(req, timeout=15) as r:
                return url, r.status
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 400, 429, 501):
                continue
            return url, e.code
        except Exception as e:
            if method == "HEAD":
                continue
            return url, f"unreachable ({type(e).__name__})"
    return url, "unreachable"


def is_broken(status):
    return status in (404, 410) or (isinstance(status, str) and "unreachable" in status)


def check_links(d):
    urls = {}
    for s in d.get("stories") or []:
        for x in s.get("src") or []:
            if isinstance(x, list) and len(x) == 3:
                urls.setdefault(x[2], []).append(f"{s.get('id')} ({x[0]})")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(probe, urls))
        # Brand-new articles often 404 for a few minutes after a site publishes them, and news CDNs
        # sometimes 404 datacenter IPs. Re-check those after a pause before calling them broken.
        retry = [u for u, st in results.items() if is_broken(st)]
        for wait in RETRY_WAITS:
            if not retry:
                break
            print(f"Re-checking {len(retry)} link(s) in {wait}s…", flush=True)
            time.sleep(wait)
            results.update(pool.map(probe, retry))
            retry = [u for u in retry if is_broken(results[u])]
    broken, blocked = [], []
    for url, status in results.items():
        if is_broken(status):
            broken.append(f"{status}: {url} — used by {', '.join(urls[url])}")
        elif isinstance(status, int) and status >= 400:
            blocked.append(f"{status}: {url}")
    return broken, blocked, len(urls)


def main():
    with open(os.path.join(ROOT, "data", "edition.json"), encoding="utf-8") as f:
        d = json.load(f)
    errors, warnings = check_structure(d)
    errors += check_fresh(d)
    broken, blocked, n = [], [], 0
    if "--links" in sys.argv:
        broken, blocked, n = check_links(d)
        errors += [f"Broken source link — {b}" for b in broken]

    lines = [f"**Edition:** {d.get('label', '?')} · {d.get('updatedAt', '?')} · {len(d.get('stories') or [])} stories", ""]
    if errors:
        lines += ["### Problems", *[f"- {e}" for e in errors], ""]
    if warnings:
        lines += ["### Warnings", *[f"- {w}" for w in warnings], ""]
    if "--links" in sys.argv:
        lines.append(f"Checked {n} source links: {len(broken)} broken, {len(blocked)} couldn't be verified (site blocks automated checks).")
        if blocked:
            lines += ["<details><summary>Couldn't verify</summary>", "", *[f"- {b}" for b in blocked], "</details>"]
    report = "\n".join(lines)
    with open(os.path.join(ROOT, "check-report.md"), "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
