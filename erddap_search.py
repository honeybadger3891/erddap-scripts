#!/usr/bin/env python3
"""Search ERDDAP servers for datasets matching a query (e.g. 'ontario').

Usage:
    python3 erddap_search.py ontario
    python3 erddap_search.py ontario --servers glerl,glos --json out.json
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

SERVERS = {
    "glerl": "https://apps.glerl.noaa.gov/erddap",
    "glos": "https://seagull-erddap.glos.org/erddap",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "erddap-scripts/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def parse_search_html(txt: str):
    """Extract (datasetID, title) pairs from an ERDDAP search results page.

    Per-dataset row layout: the title is an unclosed <td> immediately before
    the QuestionMark image cell, and the row links to .../info/<ID>/index.html.
    Walk backwards from each info link to the QuestionMark cell, then to the
    title cell — robust to the entity-encoded URLs and nested markup in the
    Tip() onmouseover attribute.
    """
    pairs = []
    for m in re.finditer(r'/info/([A-Za-z0-9_.\-]+)/index\.html', txt):
        pre = txt[: m.start()]
        q = pre.rfind("QuestionMark")
        if q == -1:
            continue
        t_mark = pre.rfind("<td>", 0, q)   # QuestionMark cell's own <td>
        if t_mark == -1:
            continue
        t_title = pre.rfind("<td>", 0, t_mark)  # title cell's <td>
        if t_title == -1:
            continue
        tm = re.match(r"<td>([^<\n]+)", txt[t_title:])
        if not tm:
            continue
        pairs.append((m.group(1), tm.group(1).strip()))
    seen, out = set(), []
    for dsid, title in pairs:
        if dsid not in seen:
            seen.add(dsid)
            out.append((dsid, title))
    return out


def search(server_name: str, base: str, query: str):
    url = base + "/search/index.html?searchFor=" + urllib.parse.quote(query)
    return parse_search_html(fetch(url))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", help="search term, e.g. 'ontario'")
    ap.add_argument("--servers", default=",".join(SERVERS),
                    help="comma-separated server names or base URLs")
    ap.add_argument("--json", dest="json_out", help="write results as JSON")
    args = ap.parse_args()

    results = []
    for name in args.servers.split(","):
        name = name.strip()
        base = (SERVERS.get(name) or name).rstrip("/")
        short = name if name in SERVERS else base
        try:
            found = search(name, base, args.query)
        except Exception as e:
            print(f"[{short}] ERROR: {e}", file=sys.stderr)
            continue
        print(f"=== {short}: {len(found)} datasets matching '{args.query}' ===")
        for dsid, title in found:
            print(f"  {dsid:40s} {title}")
            results.append({"server": short, "id": dsid, "title": title})
        print()

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Wrote {len(results)} results to {args.json_out}")


if __name__ == "__main__":
    main()
