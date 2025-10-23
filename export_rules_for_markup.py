#!/usr/bin/env python3
"""
Export rules for files under a given source path from business_rules.json.

Examples
--------
# Basic (prints to stdout, plain text like your sample)
python export_rules_for_path.py code/sf

# Specify a different rules file and write Markdown to a file
python export_rules_for_path.py code/sf \
  --rules-file /path/to/business_rules.json \
  --format md \
  --output rules_in_sf.md
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import OrderedDict, defaultdict
from pathlib import PurePosixPath, Path
from typing import Any, Dict, List, Iterable

# Constants for default argument values
DEFAULT_FORMAT = "json"
DEFAULT_OUTPUT = "./.tmp/rules-for-markup/exported-rules.json"
DEFAULT_RULES_FILE = "~/.model/business_rules.json"

def normalize_posix(p: str) -> str:
    """Normalize to POSIX-style path without leading './'."""
    # Treat provided paths as POSIX-ish (business_rules.json uses forward slashes)
    pp = PurePosixPath(p)
    # PurePosixPath('.') -> '.': avoid that
    s = str(pp)
    if s.startswith("./"):
        s = s[2:]
    return s

def path_is_under(source_prefix: str, target: str, case_insensitive: bool) -> bool:
    """Return True if target path is under source prefix (prefix on parts)."""
    sp = PurePosixPath(source_prefix)
    tp = PurePosixPath(target)
    if case_insensitive:
        sp_parts = [part.lower() for part in sp.parts]
        tp_parts = [part.lower() for part in tp.parts]
    else:
        sp_parts = list(sp.parts)
        tp_parts = list(tp.parts)
    return len(tp_parts) >= len(sp_parts) and tp_parts[:len(sp_parts)] == sp_parts

def load_rules(rules_file: Path) -> List[Dict[str, Any]]:
    try:
        with rules_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("business_rules.json must contain a top-level JSON array.")
        return data
    except FileNotFoundError:
        print(f"ERROR: rules file not found: {rules_file}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse JSON ({rules_file}): {e}", file=sys.stderr)
        sys.exit(1)

def dedupe_in_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for s in items:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out

def group_rules_by_file(
    rules: List[Dict[str, Any]],
    source_prefix: str,
    case_insensitive: bool,
    include_archived: bool = False,
) -> "OrderedDict[str, List[str]]":
    """
    Group rule_names by code_file for files under source_prefix.
    Maintain first-seen order of files and rules.
    """
    grouped: "OrderedDict[str, List[str]]" = OrderedDict()
    for r in rules:
        if not include_archived and r.get("archived") is True:
            continue
        code_file = r.get("code_file")
        rule_name = r.get("rule_name")
        if not code_file or not rule_name:
            continue
        cf_norm = normalize_posix(code_file)
        if path_is_under(source_prefix, cf_norm, case_insensitive):
            if cf_norm not in grouped:
                grouped[cf_norm] = []
            grouped[cf_norm].append(rule_name)

    # de-duplicate rule names per file, preserving order
    for cf in list(grouped.keys()):
        grouped[cf] = dedupe_in_order(grouped[cf])

    return grouped

def format_plain(grouped: "OrderedDict[str, List[str]]") -> str:
    lines: List[str] = []
    for code_file, rule_names in grouped.items():
        lines.append(f"{code_file}:")
        for rn in rule_names:
            lines.append(rn)
        lines.append("")  # blank line between files
    # Remove trailing blank if present
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)

def format_md(grouped: "OrderedDict[str, List[str]]") -> str:
    lines: List[str] = []
    for code_file, rule_names in grouped.items():
        lines.append(f"### `{code_file}`")
        for rn in rule_names:
            lines.append(f"- {rn}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)

def format_json(grouped: "OrderedDict[str, List[str]]") -> str:
    # Convert OrderedDict to normal dict for JSON output (ordering preserved in Python 3.7+)
    return json.dumps(grouped, indent=2, ensure_ascii=False)

def main():
    parser = argparse.ArgumentParser(
        description="Export business rules grouped by code_file for a given source path."
    )
    parser.add_argument(
        "source_path",
        help="Source path prefix to filter files (e.g., 'code/sf', 'code/stored_proc')."
    )
    parser.add_argument(
        "--rules-file",
        default=DEFAULT_RULES_FILE,
        help=f"Path to business_rules.json (default: {DEFAULT_RULES_FILE}; '~' will be expanded)"
    )
    parser.add_argument(
        "--format",
        choices=["plain", "md", "json"],
        default=DEFAULT_FORMAT,
        help=f"Output format (default: {DEFAULT_FORMAT})."
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Write output to this file (default: {DEFAULT_OUTPUT})."
    )
    parser.add_argument(
        "--case-insensitive",
        action="store_true",
        help="Match paths case-insensitively."
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived rules (by default they are skipped)."
    )

    args = parser.parse_args()
    source_prefix = normalize_posix(args.source_path)
    rules_file = Path(args.rules_file).expanduser()
    print(f"[info] Using rules file: {rules_file}")

    rules = load_rules(rules_file)
    print(f"[info] Loaded {len(rules)} rules")

    grouped = group_rules_by_file(rules, source_prefix, args.case_insensitive, args.include_archived)
    print(f"[info] Found {len(grouped)} files under {source_prefix}")

    if args.format == "plain":
        text = format_plain(grouped)
    elif args.format == "md":
        text = format_md(grouped)
    else:
        text = format_json(grouped)

    if args.output:
        print(f"[info] Writing output to {args.output}")
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    else:
        print(f"[info] Printing output to stdout")
        print(text)

if __name__ == "__main__":
    main()