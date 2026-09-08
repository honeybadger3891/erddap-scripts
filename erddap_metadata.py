"""Read-only, byte-preserving planning for explicit ERDDAP XML metadata.

This module deliberately does not write files or infer an ERDDAP installation.
Only administrator-selected String attributes in real addAttributes blocks are
eligible. Everything read, including unchanged XInclude dependencies, is hashed.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit
from xml.parsers import expat


class PlanError(ValueError):
    """The input cannot safely produce a complete metadata plan."""


DEFAULT_ATTRIBUTES = (
    "title", "summary", "keywords", "long_name", "comment", "description",
    "acknowledgement", "acknowledgment", "institution", "project",
)
_XI = "http://www.w3.org/2001/XInclude}"
_REMOTE_TYPES = {"EDDGridFromErddap", "EDDTableFromErddap"}
_FORBIDDEN_ATTRIBUTES = {"datasetid", "sourcename", "destinationname"}
_PROTECTED = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://|mailto:|www\.)[^\s<>\"']+"
    r"|(?<!\w)(?:/|\.\.?/|~/|[A-Za-z]:[\\/])[^\s<>\"']+"
)


def _validate_names(old: str, new: str, case_sensitive: bool) -> None:
    if not isinstance(old, str) or not isinstance(new, str):
        raise PlanError("Old and new names must be strings.")
    if not old.strip() or not new.strip():
        raise PlanError("Old and new names must not be empty.")
    if old != old.strip() or new != new.strip():
        raise PlanError("Names must not have leading or trailing whitespace.")
    if old == new or (not case_sensitive and old.casefold() == new.casefold()):
        raise PlanError("Old and new names must differ.")
    if any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or ord(c) in (0xFFFE, 0xFFFF)
           for c in old + new):
        raise PlanError("Names must not contain control or invalid XML characters.")


def _pattern(old: str, case_sensitive: bool) -> re.Pattern:
    left = r"(?<!\w)" if re.match(r"\w", old[0]) else ""
    right = r"(?!\w)" if re.match(r"\w", old[-1]) else ""
    return re.compile(left + re.escape(old) + right, 0 if case_sensitive else re.IGNORECASE)


def _replacement(matched: str, new: str, case_sensitive: bool) -> str:
    if case_sensitive:
        return new
    if matched.isupper():
        return new.upper()
    if matched.islower():
        return new.lower()
    return new


def _matches(text: str, old: str, case_sensitive: bool):
    protected = [(m.start(), m.end()) for m in _PROTECTED.finditer(text)]
    for match in _pattern(old, case_sensitive).finditer(text):
        blocked = any(match.start() < end and match.end() > start for start, end in protected)
        yield match, blocked


def replace_phrase(text: str, old: str, new: str, case_sensitive: bool = False) -> str:
    """Replace bounded phrases, preserving upper/lower case and URL/path tokens."""
    _validate_names(old, new, case_sensitive)
    result = text
    for match, blocked in reversed(list(_matches(text, old, case_sensitive))):
        if not blocked:
            result = result[:match.start()] + _replacement(match[0], new, case_sensitive) + result[match.end():]
    return result


def apply_edits(data: bytes, edits: list) -> bytes:
    """Apply verified, non-overlapping UTF-8 byte spans without touching other bytes."""
    ordered = sorted(edits, key=lambda e: (e["start"], e["end"]))
    previous_end = 0
    for edit in ordered:
        start, end = edit["start"], edit["end"]
        if (type(start) is not int or type(end) is not int or start < previous_end
                or end < start or end > len(data)):
            raise PlanError("Invalid or overlapping byte edits.")
        if data[start:end] != edit["before"].encode("utf-8"):
            raise PlanError("Edit content does not match the original bytes.")
        previous_end = end
    result = data
    for edit in reversed(ordered):
        result = result[:edit["start"]] + edit["after"].encode("utf-8") + result[edit["end"]:]
    return result


class _Node:
    def __init__(self, tag, attrs, path, start, inner_start, empty):
        self.tag, self.attrs, self.path = tag, attrs, path
        self.start, self.inner_start, self.empty = start, inner_start, empty
        self.inner_end = inner_start
        self.children = []
        self.text = []
        self.content = []

    def all_text(self):
        return "".join(item if isinstance(item, str) else item.all_text() for item in self.content)


def _tag_end(data: bytes, start: int) -> int:
    quote = None
    for offset in range(start, len(data)):
        char = data[offset]
        if quote:
            if char == quote:
                quote = None
        elif char in (34, 39):
            quote = char
        elif char == 62:
            return offset + 1
    raise PlanError("Unterminated XML start tag.")


def _parse(data: bytes, path: str) -> _Node:
    try:
        data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise PlanError(f"{path}: only UTF-8 XML is supported.") from exc
    parser = expat.ParserCreate(namespace_separator="}")
    stack, roots = [], []

    def refuse(*args):
        raise PlanError(f"{path}: DTDs and entity declarations are not supported.")

    def declaration(version, encoding, standalone):
        if encoding and encoding.lower().replace("-", "") != "utf8":
            raise PlanError(f"{path}: only UTF-8 XML is supported, not {encoding}.")
        if version != "1.0":
            raise PlanError(f"{path}: only XML 1.0 is supported.")

    def namespace(prefix, uri):
        if uri and uri != _XI[:-1]:
            raise PlanError(f"{path}: unsupported XML namespace {uri!r}.")

    def start(tag, attrs):
        if "}" in tag and tag != _XI + "include":
            raise PlanError(f"{path}: unsupported namespaced element {tag!r}.")
        if any("}" in name for name in attrs):
            raise PlanError(f"{path}: namespaced attributes (including xml:base) are unsupported.")
        inner_start = _tag_end(data, parser.CurrentByteIndex)
        empty = data[parser.CurrentByteIndex:inner_start].rstrip().endswith(b"/>")
        node = _Node(tag, dict(attrs), path, parser.CurrentByteIndex, inner_start, empty)
        if stack:
            stack[-1].children.append(node)
            stack[-1].content.append(node)
        else:
            roots.append(node)
        stack.append(node)

    def end(tag):
        node = stack.pop()
        node.inner_end = node.inner_start if node.empty else parser.CurrentByteIndex

    def characters(value):
        if stack:
            stack[-1].text.append(value)
            stack[-1].content.append(value)

    parser.XmlDeclHandler = declaration
    parser.StartNamespaceDeclHandler = namespace
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = characters
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise PlanError(f"{path}: invalid XML: {exc}.") from exc
    if len(roots) != 1:
        raise PlanError(f"{path}: expected one XML root element.")
    return roots[0]


def _read(path: Path) -> bytes:
    try:
        for component in [*reversed(path.parents), path]:
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PlanError(f"{path}: symlinks are unsupported ({component}).")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PlanError(f"{path}: expected a regular file with one hard link.")
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                raise PlanError(f"{path}: file changed while opening it.")
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PlanError(f"{path}: expected a regular file with one hard link.")
            data = source.read()
            after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise PlanError(f"{path}: file changed while reading it.")
        return data
    except OSError as exc:
        raise PlanError(f"Cannot read {path}: {exc}.") from exc


def _absolute_path(value: str) -> str:
    # Check the original spelling before normalizing '..': a symlink followed
    # by '..' can otherwise select a different file from the one the OS opens.
    spelled = Path(os.path.join(os.getcwd(), value))
    try:
        for component in [*reversed(spelled.parents), spelled]:
            if stat.S_ISLNK(component.lstat().st_mode):
                raise PlanError(f"{spelled}: symlinks are unsupported ({component}).")
    except (OSError, ValueError) as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError(f"Cannot inspect {spelled}: {exc}.") from exc
    return os.path.abspath(str(spelled))


def _decode_segment(raw: bytes, offset: int, mode: str, segment: int):
    """Map each XML-decoded character to its original byte span."""
    chars, spans = [], []
    cursor = 0
    entities = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
    while cursor < len(raw):
        start = cursor
        if raw[cursor:cursor + 1] == b"&" and mode == "text":
            cursor = raw.index(b";", cursor) + 1
            entity = raw[start + 1:cursor - 1].decode("ascii")
            if entity.startswith("#x"):
                char = chr(int(entity[2:], 16))
            elif entity.startswith("#"):
                char = chr(int(entity[1:]))
            else:
                char = entities[entity]
        elif raw[cursor:cursor + 1] == b"\r":
            cursor += 2 if raw[cursor:cursor + 2] == b"\r\n" else 1
            char = "\n"
        else:
            size = 1
            while cursor + size < len(raw) and raw[cursor + size] & 0xC0 == 0x80:
                size += 1
            char = raw[cursor:cursor + size].decode("utf-8")
            cursor += size
        chars.append(char)
        spans.append((offset + start, offset + cursor, mode, segment))
    return chars, spans


def _text_spans(node: _Node, data: bytes):
    raw = data[node.inner_start:node.inner_end]
    token = re.compile(br"<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\?[\s\S]*?\?>")
    chars, spans, cursor, segment = [], [], 0, 0
    for match in list(token.finditer(raw)) + [None]:
        stop = match.start() if match else len(raw)
        cs, ss = _decode_segment(raw[cursor:stop], node.inner_start + cursor, "text", segment)
        chars.extend(cs)
        spans.extend(ss)
        segment += 1
        if match is None:
            break
        if match[0].startswith(b"<![CDATA["):
            cs, ss = _decode_segment(match[0][9:-3], node.inner_start + match.start() + 9, "cdata", segment)
            chars.extend(cs)
            spans.extend(ss)
        segment += 1
        cursor = match.end()
    return "".join(chars), spans


def build_plan(paths: list[str], old: str, new: str, *, case_sensitive: bool = False,
               attributes: list[str] | None = None) -> dict:
    """Inventory the full local include graph and plan only approved metadata edits.

    ``attributes`` extends the default allowlist. Include targets must remain
    below the directory containing their selected root datasets.xml. Includes
    with fallback, XPointer, parse=text, remote URLs, or xml:base are rejected.
    """
    _validate_names(old, new, case_sensitive)
    effective = set(DEFAULT_ATTRIBUTES)
    for name in attributes or []:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", name):
            raise PlanError(f"Invalid metadata attribute name: {name!r}.")
        if name.lower() in _FORBIDDEN_ATTRIBUTES or re.search(r"(?:url|uri|path)$", name, re.I):
            raise PlanError(f"Identifier, URL, and path attributes cannot be selected: {name}.")
        effective.add(name)
    if not paths:
        raise PlanError("Select at least one active datasets.xml path.")
    roots = sorted({_absolute_path(str(path)) for path in paths})
    cache, datasets, occurrences, manual, seen_dataset_ids = {}, [], [], [], {}

    def load(path):
        if path not in cache:
            data = _read(Path(path))
            cache[path] = {"data": data, "node": _parse(data, path)}
        return cache[path]["node"]

    def report(node, context, attribute, before, reason, **extra):
        manual.append({"path": node.path, "dataset_id": context.get("dataset_id"),
                       "variable": context.get("variable"), "attribute": attribute,
                       "before": before, "reason": reason, **extra})

    def walk(node, ancestors, context, chain, root):
        # The expanded ancestry follows XInclude replacement semantics.
        if node.tag == _XI + "include":
            allowed = {"href", "parse"}
            if set(node.attrs) - allowed or node.attrs.get("parse", "xml") != "xml" or node.children or "".join(node.text).strip():
                raise PlanError(f"{node.path}: unsupported XInclude options or fallback.")
            href = node.attrs.get("href", "")
            try:
                parts = urlsplit(href)
            except ValueError as exc:
                raise PlanError(f"{node.path}: invalid include URI {href!r}.") from exc
            if (not href or parts.scheme or parts.netloc or parts.query or parts.fragment
                    or href.startswith("/") or "\\" in href):
                raise PlanError(f"{node.path}: only relative local XML include paths are supported: {href!r}.")
            decoded = unquote(parts.path)
            if decoded.startswith("/") or "\\" in decoded or "\0" in decoded:
                raise PlanError(f"{node.path}: invalid local include path {href!r}.")
            spelled_target = os.path.join(os.path.dirname(node.path), decoded)
            target = os.path.abspath(spelled_target)
            boundary = os.path.dirname(root)
            if os.path.commonpath([target, boundary]) != boundary:
                raise PlanError(f"{node.path}: include escapes the selected configuration directory: {href!r}.")
            target = _absolute_path(spelled_target)
            if target in chain:
                raise PlanError(f"{node.path}: cyclic XInclude involving {target}.")
            if len(chain) >= 64:
                raise PlanError(f"{node.path}: XInclude nesting exceeds 64 files.")
            if _pattern(old, case_sensitive).search(href):
                report(node, context, "@href", href, "include_path")
            walk(load(target), ancestors, context, chain + (target,), root)
            return

        context = dict(context)
        parent_tag = ancestors[-1] if ancestors else None
        if node.tag == "erddapDatasets" and ancestors:
            raise PlanError(f"{node.path}: an included document cannot introduce a nested erddapDatasets root.")
        if node.tag == "dataset":
            if parent_tag not in {"erddapDatasets", "dataset"}:
                raise PlanError(f"{node.path}: dataset appears outside an ERDDAP dataset container.")
            dataset_id = node.attrs.get("datasetID", "")
            if not dataset_id:
                raise PlanError(f"{node.path}: dataset has no datasetID.")
            if dataset_id in seen_dataset_ids[root]:
                raise PlanError(f"{node.path}: duplicate datasetID {dataset_id!r} in {root}.")
            seen_dataset_ids[root].add(dataset_id)
            parent_id = context.get("dataset_id")
            context.update(dataset_id=dataset_id, top_level_id=context.get("top_level_id") or dataset_id,
                           variable=None, upstream=context.get("upstream", False) or node.attrs.get("type") in _REMOTE_TYPES)
            if parent_id is None:
                context["top_level_active"] = node.attrs.get("active", "true").lower() != "false"
            datasets.append({"root": root, "path": node.path, "dataset_id": dataset_id,
                             "type": node.attrs.get("type", ""), "parent_dataset_id": parent_id,
                             "top_level_id": context["top_level_id"],
                             "active": node.attrs.get("active", "true").lower() != "false",
                             "upstream_required": context["upstream"]})
        if node.tag in {"dataVariable", "axisVariable"} and parent_tag == "dataset":
            sources = [child for child in node.children if child.tag == "sourceName"]
            context["variable"] = sources[0].all_text() if len(sources) == 1 else "(unnamed variable)"
        for attr_name, value in sorted(node.attrs.items()):
            if _pattern(old, case_sensitive).search(value):
                report(node, context, "@" + attr_name, value, "structural_attribute")

        if node.tag == "att":
            value = node.all_text()
            if _pattern(old, case_sensitive).search(value):
                name = node.attrs.get("name", "")
                valid_parent = (len(ancestors) >= 2 and parent_tag == "addAttributes"
                                and (ancestors[-2] == "dataset" or (
                                    ancestors[-2] in {"dataVariable", "axisVariable"}
                                    and len(ancestors) >= 3 and ancestors[-3] == "dataset"))
                                and context.get("dataset_id"))
                if context.get("upstream"):
                    reason = "upstream_required"
                elif "sourceAttributes" in ancestors:
                    reason = "source_attribute_requires_override"
                elif not valid_parent:
                    reason = "outside_metadata_addAttributes"
                elif node.attrs.get("type", "String") != "String":
                    reason = "non_string_attribute"
                elif name not in effective:
                    reason = "attribute_not_selected"
                elif node.children:
                    reason = "nested_markup_in_attribute"
                else:
                    reason = None
                occurrences.append((node, context, name, value, reason))
        elif not node.children and node.tag not in {"sourceName", "destinationName"}:
            value = "".join(node.text)
            if _pattern(old, case_sensitive).search(value):
                report(node, context, node.tag, value, "outside_metadata_addAttributes")
        elif node.tag in {"sourceName", "destinationName"}:
            value = node.all_text()
            if _pattern(old, case_sensitive).search(value):
                report(node, context, node.tag, value, "variable_identifier")
        for child in node.children:
            walk(child, ancestors + [node.tag], context, chain, root)

    for root in roots:
        seen_dataset_ids[root] = set()
        document = load(root)
        if document.tag != "erddapDatasets":
            raise PlanError(f"{root}: expected an erddapDatasets root; select the active datasets.xml.")
        walk(document, [], {}, (root,), root)

    # A shared file must be editable in every context in which it is included.
    blocked = {(node.path, node.start) for node, ctx, name, value, reason in occurrences if reason}
    edits_by_path, changes, reload_ids = {}, [], set()
    for node, context, name, value, reason in occurrences:
        if reason or (node.path, node.start) in blocked:
            report(node, context, name, value, reason or "shared_include_has_noneditable_context")
            continue
        data = cache[node.path]["data"]
        value, spans = _text_spans(node, data)
        matches = list(_matches(value, old, case_sensitive))
        selected, rejections = [], set()
        for match, protected in matches:
            first, last = spans[match.start()], spans[match.end() - 1]
            if protected:
                rejections.add("url_or_path_reference")
            elif first[3] != last[3]:
                rejections.add("phrase_spans_markup")
            else:
                replacement = _replacement(match[0], new, case_sensitive)
                encoded = (replacement.replace("]]>", "]]]]><![CDATA[>") if first[2] == "cdata"
                           else replacement.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
                selected.append((match, {"start": first[0], "end": last[1],
                                        "before": data[first[0]:last[1]].decode("utf-8"), "after": encoded}))
        for rejection in sorted(rejections):
            report(node, context, name, value, rejection)
        if selected:
            after = value
            for match, edit in reversed(selected):
                after = after[:match.start()] + _replacement(match[0], new, case_sensitive) + after[match.end():]
                edits_by_path.setdefault(node.path, {})[(edit["start"], edit["end"])] = edit
            changes.append({"path": node.path, "dataset_id": context["dataset_id"],
                            "top_level_id": context["top_level_id"], "variable": context.get("variable"),
                            "attribute": name, "before": value, "after": after,
                            "replacement_count": len(selected)})
            if context["top_level_active"]:
                reload_ids.add(context["top_level_id"])
    files = []
    for path, entry in sorted(cache.items()):
        edits = sorted(edits_by_path.get(path, {}).values(), key=lambda e: e["start"])
        after = apply_edits(entry["data"], edits)
        _parse(after, path)
        files.append({"path": path, "before_sha256": hashlib.sha256(entry["data"]).hexdigest(),
                      "after_sha256": hashlib.sha256(after).hexdigest(), "edits": edits})
    return {"schema_version": 1, "roots": roots, "old": old, "new": new,
            "case_sensitive": case_sensitive, "attributes": sorted(effective),
            "files": files, "datasets": datasets, "changes": changes,
            "manual_review": manual, "reload_ids": sorted(reload_ids)}
