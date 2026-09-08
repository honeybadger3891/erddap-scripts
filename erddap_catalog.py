"""Read-only, paginated ERDDAP catalog and effective-metadata inspection.

The public ``/info`` service shows metadata ERDDAP currently serves, including
source attributes that may not appear in datasets.xml. Findings from this
module are review candidates, never an executable local-file change plan.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from erddap_metadata import replace_phrase


PAGE_SIZE = 100
MAX_PAGES = 10000
REQUEST_DELAY = 0.05
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class CatalogError(ValueError):
    """The server response cannot establish a complete catalog result."""

    def __init__(self, message, url=None):
        super().__init__(message)
        self.url = url


def normalize_server(server):
    """Accept an explicit HTTP(S) ERDDAP base URL without credentials."""
    if not isinstance(server, str) or any(char.isspace() for char in server):
        raise ValueError("server must be an HTTP(S) ERDDAP base URL")
    parsed = urllib.parse.urlsplit(server)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "\\" in server):
        raise ValueError("server must be an HTTP(S) base URL without credentials, query, or fragment")
    # Evaluating port also rejects malformed and out-of-range port numbers.
    parsed.port
    return server.rstrip("/")


def _fetch_table(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "erddap-scripts/1.0", "Accept": "application/json"}
    )
    time.sleep(REQUEST_DELAY)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise CatalogError("response exceeded the 32 MiB inspection limit", url)
    try:
        document = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError("expected ERDDAP JSON, received invalid JSON or HTML", url) from exc
    if not isinstance(document, dict) or not isinstance(document.get("table"), dict):
        raise CatalogError("missing ERDDAP JSON table", url)
    table = document["table"]
    names, rows = table.get("columnNames"), table.get("rows")
    if (not isinstance(names, list) or not names
            or not all(isinstance(name, str) for name in names)
            or len(set(names)) != len(names) or not isinstance(rows, list)):
        raise CatalogError("invalid ERDDAP table columns or rows", url)
    if any(not isinstance(row, list) or len(row) != len(names) for row in rows):
        raise CatalogError("ERDDAP table row width differs from its columns", url)
    return names, rows


def _known_empty_page(error, page):
    """Recognize ERDDAP's specific terminal response, never arbitrary 404s."""
    if error.code != 404:
        return False
    try:
        body = error.read(8192).decode("utf-8", "replace")
    except OSError:
        return False
    structured = re.fullmatch(
        r'\s*Error\s*\{\s*code=404;\s*message="([^"]*)";\s*\}\s*', body
    )
    if structured is None:
        return False
    message = structured.group(1)
    # ERDDAP returns this response when a full final page is followed by a
    # request for the next page. Requiring the exact previous page also avoids
    # silently declaring a changing/shrinking catalog complete.
    beyond_last = re.fullmatch(
        r"Not Found:(?: Resource not found:)? You requested results page=(\d+), but the last page is (\d+)\. Please request a page from 1 through \2\.",
        message,
    )
    if beyond_last:
        requested, last = map(int, beyond_last.groups())
        return page > 1 and requested == page and last == page - 1
    # A missing endpoint must remain an error. Only the structured ERDDAP
    # no-matches response means an empty first page.
    return page == 1 and message.startswith(
        ("Not Found: Your query produced no matching results.",
         "Not Found: Resource not found: Your query produced no matching results.")
    )


def iter_index(server, *, search_for=None):
    """Yield all visible catalog rows; raise if pagination cannot be trusted.

    ``Dataset ID`` is the actual /info/index.json column name (not
    ``datasetID``). Column order is deliberately not assumed.
    """
    server = normalize_server(server)
    seen = set()
    endpoint = "info" if search_for is None else "search"
    for page in range(1, MAX_PAGES + 1):
        query = {"page": page, "itemsPerPage": PAGE_SIZE}
        if search_for is not None:
            query["searchFor"] = search_for
        url = server + "/" + endpoint + "/index.json?" + urllib.parse.urlencode(query)
        try:
            names, rows = _fetch_table(url)
        except urllib.error.HTTPError as exc:
            if _known_empty_page(exc, page):
                return
            raise CatalogError(f"HTTP {exc.code} while reading catalog page {page}", url) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise CatalogError(f"unable to read catalog page {page}: {exc}", url) from exc
        if "Dataset ID" not in names or "Title" not in names:
            raise CatalogError("catalog is missing the Dataset ID or Title column", url)
        if len(rows) > PAGE_SIZE:
            raise CatalogError("server ignored itemsPerPage; pagination is untrustworthy", url)
        page_records = []
        page_ids = set()
        for values in rows:
            row = dict(zip(names, values))
            dataset_id, title = row["Dataset ID"], row["Title"]
            if (not isinstance(dataset_id, str) or not dataset_id
                    or dataset_id in (".", "..") or not isinstance(title, str)):
                raise CatalogError("catalog contains an invalid dataset ID or title", url)
            if dataset_id in seen or dataset_id in page_ids:
                raise CatalogError(f"duplicate dataset ID {dataset_id!r}; catalog pages did not advance reliably", url)
            page_ids.add(dataset_id)
            page_records.append(row)
        seen.update(page_ids)
        yield from page_records
        if len(rows) < PAGE_SIZE:
            return
    raise CatalogError(f"catalog exceeded the {MAX_PAGES}-page inspection limit", url)


def audit_catalog(server: str, old: str, new: str, *, case_sensitive=False):
    """Inspect every public dataset's attributes for a proposed phrase rename."""
    server = normalize_server(server)
    if not isinstance(old, str) or not old.strip() or not isinstance(new, str) or not new.strip():
        raise ValueError("old and new names must be nonempty strings")
    report = {
        "server": server,
        "old": old,
        "new": new,
        "case_sensitive": case_sensitive,
        "dataset_count": 0,
        "metadata_checked_count": 0,
        "datasets": [],
        "matches": [],
        "errors": [],
        "complete": False,
        "limitations": [
            "Read-only candidates for administrator review; this report cannot be applied as a local-file plan.",
            "Only anonymously visible datasets and currently served metadata are inspected; private, inactive, and failed datasets may be absent.",
            "Effective attributes may originate in source files or generated configuration; a public match does not identify its editable source.",
            "Data values, downloaded files, map imagery, external pages, and linked metadata documents are not inspected.",
            "The catalog may change during inspection; this report is not an atomic server snapshot.",
            "The synthetic allDatasets catalog and occurrences inside URL/path tokens are excluded from rename candidates.",
        ],
    }
    try:
        for row in iter_index(server):
            dataset_id = row["Dataset ID"]
            if dataset_id == "allDatasets":
                continue
            url = server + "/info/" + urllib.parse.quote(dataset_id, safe="") + "/index.json"
            report["datasets"].append({
                "dataset_id": dataset_id,
                "title": row["Title"],
                "url": url,
            })
    except (CatalogError, ValueError) as exc:
        report["errors"].append({"stage": "catalog", "url": getattr(exc, "url", None), "message": str(exc)})
    report["dataset_count"] = len(report["datasets"])
    for dataset in report["datasets"]:
        url = dataset["url"]
        try:
            names, rows = _fetch_table(url)
            required = ("Row Type", "Variable Name", "Attribute Name", "Data Type", "Value")
            if any(name not in names for name in required):
                raise CatalogError("metadata is missing required ERDDAP attribute columns", url)
            matches = []
            for values in rows:
                row = dict(zip(names, values))
                if row["Row Type"] != "attribute":
                    continue
                if (not isinstance(row["Variable Name"], str)
                        or not isinstance(row["Attribute Name"], str)
                        or not isinstance(row["Data Type"], str)
                        or not isinstance(row["Value"], (str, int, float))):
                    raise CatalogError("metadata contains an invalid attribute row", url)
                if row["Data Type"].lower() != "string" or not isinstance(row["Value"], str):
                    continue
                before = row["Value"]
                after = replace_phrase(before, old, new, case_sensitive=case_sensitive)
                if before != after:
                    matches.append({
                        "dataset_id": dataset["dataset_id"],
                        "variable": row["Variable Name"],
                        "attribute": row["Attribute Name"],
                        "before": before,
                        "after": after,
                        "url": url,
                    })
            report["matches"].extend(matches)
            report["metadata_checked_count"] += 1
        except (CatalogError, ValueError, urllib.error.URLError, OSError) as exc:
            report["errors"].append({
                "stage": "metadata", "dataset_id": dataset["dataset_id"], "url": url, "message": str(exc)
            })
    report["complete"] = not report["errors"]
    return report
