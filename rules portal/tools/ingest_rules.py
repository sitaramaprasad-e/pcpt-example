import re
import json
import sys
import os
import uuid
import glob
import hashlib

from datetime import datetime
from typing import Optional, List, Dict, Tuple

# ===== Execution & Artifact linking (new) =====

LOG_DIR = os.environ.get("PCPT_LOG_DIR", os.path.expanduser("~/.pcpt/log"))

def _prompt_model_home() -> str:
    try:
        resp = input("Enter model home path (default='~/'):").strip()
    except EOFError:
        # Non-interactive (e.g., piped/cron) – fall back to default
        resp = ""
    if not resp:
        resp = "~"
    return os.path.expanduser(resp)

MODEL_HOME = _prompt_model_home()

# ===== PCPT Header parsing for model/sources.json & model/runs.json (new) =====
PCPT_PREFIX  = "[PCPTLOG:]"
# Robust markers that ignore exact chevron counts; match by phrase
HEADER_BEGIN_RX = re.compile(r"HEADER\s+BEGIN")
HEADER_END_RX   = re.compile(r"HEADER\s+END")
KV_LINE = re.compile(rf"^{re.escape(PCPT_PREFIX)}\s+(?P<k>[a-zA-Z0-9_]+)=(?P<v>.*)$")
SOURCE_JSON = f"{MODEL_HOME}/.model/sources.json"
RUNS_JSON   = f"{MODEL_HOME}/.model/runs.json"
MIN_BUILD_NUM = 2510020935  # ignore headers from builds before this

# Log parsing patterns
RE_OUTPUT_REPORT = re.compile(r"^\s*Output report:\s*(?P<path>.+)\s*$", re.IGNORECASE)
RE_OUTPUT_ALSO  = re.compile(r"^\s*Output file created at:\s*(?P<path>.+)\s*$", re.IGNORECASE)
RE_FILE_HEADER  = re.compile(r"^\s*File:\s*(?P<path>.+?)\s*$")
RE_FENCE_START  = re.compile(r"^\s*```")
RE_REPORT_SAVED = re.compile(r"^\s*(Report saved|Saved report|Saving report):\s*(?P<path>.+)\s*$", re.IGNORECASE)
RE_OUTPUT_GENERIC = re.compile(r"^\s*(Output|Wrote|Saved):\s*(?P<path>.+)\s*$", re.IGNORECASE)

# Accept input file path from command line argument
if len(sys.argv) < 2:
    print("Usage: python ingest_rules.py <input_file>")
    sys.exit(1)

input_file = sys.argv[1]

file_mtime = os.path.getmtime(input_file)
file_timestamp = datetime.utcfromtimestamp(file_mtime).replace(microsecond=0).isoformat() + "Z"

# Helper for normalized deduplication key
def _dedupe_key(rule_name, timestamp):
    """Build a stable key from rule_name and timestamp with normalization."""
    if rule_name is None or timestamp is None:
        return None
    rn = str(rule_name).strip()
    ts = str(timestamp).strip()
    # Normalize timestamp to drop microseconds
    if '.' in ts:
        ts = ts.split('.')[0] + 'Z'
    return (rn, ts)

def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _save_json_file(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

# ===== PCPT Header parsing helpers for model/sources.json & model/runs.json (new) =====
def _coerce_header_value(v: str):
    s = (v or "").strip()
    # Try JSON first (arrays/objects/strings/numbers)
    try:
        return json.loads(s)
    except Exception:
        pass
    # Strip surrounding quotes if present
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s

def _parse_pcpt_header_block(lines):
    data = {}
    for line in lines:
        m = KV_LINE.match(line.rstrip("\n"))
        if not m:
            continue
        k, v = m.group("k"), m.group("v")
        data[k] = _coerce_header_value(v)
    return data

def _iter_pcpt_header_blocks(text: str):
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        # Look for a BEGIN line that contains the phrase
        if HEADER_BEGIN_RX.search(lines[i]):
            i += 1
            block = []
            # Collect until END phrase
            while i < len(lines) and not HEADER_END_RX.search(lines[i]):
                if lines[i].strip():
                    block.append(lines[i])
                i += 1
            # Skip the END line if present
            if i < len(lines) and HEADER_END_RX.search(lines[i]):
                i += 1
            if block:
                yield _parse_pcpt_header_block(block)
            continue
        i += 1

def _build_sources_and_runs_from_logs(log_paths):
    sources = {}  # root_dir -> set(source_paths)
    runs = []
    for log_path in log_paths:
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            continue
        for hdr in _iter_pcpt_header_blocks(text):
            # Skip headers from older builds, and skip if build is unknown
            build_raw = hdr.get("build")
            build_num = None
            if build_raw is not None:
                try:
                    build_num = int(str(build_raw).strip())
                except Exception:
                    build_num = None
            # Now ignore if unknown or below threshold
            if build_num is None or build_num < MIN_BUILD_NUM:
                continue
            root_dir = hdr.get("root_dir")
            source_path = hdr.get("source_path")
            output_path = hdr.get("output_path")
            input_files = hdr.get("input_files") or []
            output_file = hdr.get("output_file")
            prompt = hdr.get("prompt") or hdr.get("prompt_template")
            if root_dir and source_path:
                sources.setdefault(root_dir, set()).add(str(source_path))
            runs.append({
                "timestamp": hdr.get("timestamp"),
                "build": hdr.get("build"),
                "mode": hdr.get("mode"),
                "provider": hdr.get("provider"),
                "model": hdr.get("model"),
                "prompt": prompt,
                "source_path": source_path,
                "output_path": output_path,
                "input_files": input_files,
                "output_file": output_file,
                "root_dir": root_dir,
                "log_file": str(log_path),
            })
    sources_out = [
        {"root_dir": rd, "source_paths": sorted(list(paths))}
        for rd, paths in sorted(sources.items(), key=lambda x: x[0])
    ]
    return sources_out, runs

def _discover_pcpt_header_files() -> list:
    """Return files that likely contain PCPT headers.
    We search LOG_DIR, the current working directory, and the directory of the input file
    (plus its parent) for common text extensions. We quickly pre-check content for the markers
    to avoid scanning big binaries.
    """
    exts = (".log", ".txt", ".out", ".md")
    candidates = set()

    # 1) LOG_DIR
    for pat in ("**/*.log", "**/*.txt", "**/*.out", "**/*.md"):
        candidates.update(glob.glob(os.path.join(LOG_DIR, pat), recursive=True))

    # 2) CWD
    def _walk_add(base: str):
        if not base or not os.path.isdir(base):
            return
        for root, _, files in os.walk(base):
            for name in files:
                if name.endswith(exts):
                    candidates.add(os.path.join(root, name))

    _walk_add(os.getcwd())

    # 3) input file dir and its parent (to catch reports under repo root)
    try:
        in_dir = os.path.dirname(os.path.abspath(input_file))
        _walk_add(in_dir)
        _walk_add(os.path.dirname(in_dir))
    except Exception:
        pass

    files = []
    for p in sorted(candidates):
        # Quick pre-check: look for markers near the top
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(8192)
            if PCPT_PREFIX in head and "HEADER" in head:
                files.append(p)
        except Exception:
            continue
    return files


# Helper to match a run record's output_file to a given target path
def _matches_output_file(run_rec: dict, target_path: str) -> bool:
    """Return True if the run's output_file plausibly refers to target_path.
    We compare absolute forms and tolerant suffix matches using root_dir/output_path hints.
    """
    if not target_path or not run_rec:
        return False
    try:
        abs_target = os.path.abspath(target_path)
        of = str(run_rec.get("output_file") or "").strip()
        if not of:
            return False
        # Normalize known pieces
        root_dir = run_rec.get("root_dir") or ""
        output_path = run_rec.get("output_path") or ""
        cands = set()
        of_norm = os.path.normpath(of)
        cands.add(of_norm)
        cands.add(os.path.abspath(of_norm))
        if root_dir and output_path:
            cands.add(os.path.abspath(os.path.join(root_dir, output_path, of_norm)))
        if root_dir:
            cands.add(os.path.abspath(os.path.join(root_dir, of_norm)))
        if output_path:
            cands.add(os.path.abspath(os.path.join(output_path, of_norm)))
        # Direct absolute equality
        if abs_target in cands:
            return True
        # Tolerant suffix match (e.g., abs path ends with relative stored in log)
        for cand in list(cands):
            cand_norm = os.path.normpath(str(cand))
            if abs_target.endswith(os.sep + cand_norm) or cand_norm.endswith(os.sep + os.path.basename(abs_target)):
                return True
        return False
    except Exception:
        return False

def write_model_sources_and_runs(rule_ids_for_output: Optional[List[str]] = None, output_file_path: Optional[str] = None):
    """Scan logs/reports for PCPT headers and emit .model/sources.json and .model/runs.json.
    If `rule_ids_for_output` and `output_file_path` are provided, attach the list of rule IDs
    to any run records whose `output_file` matches `output_file_path` as `rule_ids`.
    """
    logs = _discover_pcpt_header_files()
    if os.environ.get("PCPT_DEBUG"):
        print(f"[DEBUG] PCPT header candidate files: {len(logs)}")
        for p in logs[:15]:
            print(f"[DEBUG]  - {p}")
    sources_out, runs = _build_sources_and_runs_from_logs(logs)
    # Preserve previously stored rule_ids from existing runs.json before we add new links
    try:
        _existing_runs = []
        if os.path.exists(RUNS_JSON) and os.path.getsize(RUNS_JSON) > 0:
            with open(RUNS_JSON, "r", encoding="utf-8") as rf:
                _existing_runs = json.load(rf) or []
    except Exception:
        _existing_runs = []

    def _run_key(rec: dict) -> Tuple[str, str]:
        # Use (timestamp, log_file) as a stable identity; both are emitted by header parsing
        return (str(rec.get("timestamp") or ""), str(rec.get("log_file") or ""))

    _existing_map: Dict[Tuple[str, str], dict] = { _run_key(r): r for r in _existing_runs if isinstance(r, dict) }

    # Copy forward any existing rule_ids so we don't lose them when we rebuild from logs
    for rec in runs:
        k = _run_key(rec)
        prev = _existing_map.get(k)
        if prev and isinstance(prev, dict):
            prev_ids = prev.get("rule_ids")
            if prev_ids and not rec.get("rule_ids"):
                try:
                    rec["rule_ids"] = list(prev_ids)
                except Exception:
                    rec["rule_ids"] = prev_ids
    # Optionally attach rule IDs for the current run based on the produced report path
    if rule_ids_for_output and output_file_path:
        try:
            ids_list = list(rule_ids_for_output)
        except Exception:
            ids_list = None
        if ids_list:
            # Collect all runs that match this output file
            matched_idxs = []
            for idx, rec in enumerate(runs):
                if _matches_output_file(rec, output_file_path):
                    matched_idxs.append(idx)
            if matched_idxs:
                # Find most recent by timestamp (ISO8601). If parse fails, prefer later index (assumed newer).
                def _parse_ts(ts: str):
                    try:
                        # Support 'YYYY-MM-DDTHH:MM:SSZ' and similar without timezone
                        t = str(ts or "").strip()
                        if not t:
                            return None
                        # Remove trailing 'Z' for fromisoformat
                        if t.endswith('Z'):
                            t = t[:-1]
                        return datetime.fromisoformat(t)
                    except Exception:
                        return None
                newest_idx = matched_idxs[0]
                newest_dt = _parse_ts(runs[newest_idx].get("timestamp"))
                for idx in matched_idxs[1:]:
                    dt = _parse_ts(runs[idx].get("timestamp"))
                    if newest_dt is None and dt is not None:
                        newest_idx, newest_dt = idx, dt
                    elif dt is not None and newest_dt is not None and dt > newest_dt:
                        newest_idx, newest_dt = idx, dt
                    elif dt is None and newest_dt is None and idx > newest_idx:
                        # Fallback: later occurrence considered newer
                        newest_idx = idx
                # Do NOT remove rule_ids from older runs. Only add to the most recent run.
                existing = runs[newest_idx].get("rule_ids") or []
                try:
                    # Preserve order: existing first, then new ones that aren't already present
                    combined = list(existing) + [rid for rid in ids_list if rid not in set(existing)]
                except Exception:
                    combined = ids_list
                runs[newest_idx]["rule_ids"] = combined
    os.makedirs(os.path.dirname(SOURCE_JSON), exist_ok=True)
    _save_json_file(SOURCE_JSON, sources_out)
    _save_json_file(RUNS_JSON, runs)

def _all_logs() -> list:
    patterns = ["**/*.log", "**/*.txt", "**/*.out"]
    results = []
    for pat in patterns:
        results.extend(glob.glob(os.path.join(LOG_DIR, pat), recursive=True))
    return sorted(results)


def _heading_text(line: str) -> str:
    """Extract clean heading text from a markdown heading line.
    Removes leading/trailing '#' and surrounding whitespace/markers.
    """
    s = (line or "").strip()
    # remove leading hashes and spaces
    s = re.sub(r"^\s*#{1,6}\s*", "", s)
    # remove trailing hashes and spaces
    s = re.sub(r"\s*#{1,6}\s*$", "", s)
    s = s.strip(" *-\t")
    # If the heading still includes a leading label like "Rule Name:", strip it.
    s = re.sub(r"^Rule Name:\s*", "", s, flags=re.IGNORECASE)
    return s


output_file = f"{MODEL_HOME}/.model/business_rules.json"

# Read existing rules if output file already exists and is not empty
if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
    with open(output_file, "r", encoding="utf-8") as f:
        try:
            existing_rules = json.load(f)
        except json.JSONDecodeError:
            print(f"Warning: {output_file} is not valid JSON. Starting fresh.")
            existing_rules = []
else:
    existing_rules = []

existing_by_name = {
    r["rule_name"]: r
    for r in existing_rules
    if "rule_name" in r and "timestamp" in r
}

# Build set of seen (rule_name, timestamp) pairs from existing rules (normalized)
seen = set()
for rule in existing_rules:
    k = _dedupe_key(rule.get("rule_name"), rule.get("timestamp"))
    if k:
        seen.add(k)

with open(input_file, "r", encoding="utf-8") as f:
    text = f.read()

# Normalize various "Rule Name" heading formats to a consistent "## " heading
text = re.sub(r"#{2,6}\s*\d+\.\s*\*\*Rule Name:\*\*\s*", "## ", text)   # e.g., "### 1. **Rule Name:**"
text = re.sub(r"#{2,6}\s*\*\*Rule Name:\*\*\s*", "## ", text)            # e.g., "### **Rule Name:**"
text = re.sub(r"#{2,6}\s*\d+\.\s*Rule Name:\s*", "## ", text)            # e.g., "### 1. Rule Name:"
text = re.sub(r"#{2,6}\s*Rule Name:\s*", "## ", text)                    # e.g., "### Rule Name:"
text = re.sub(r"\n---+\n", "\n", text)                                   # Remove separators

# Split into rule sections
rule_sections = re.split(r"(?m)^\s{0,3}#{1,6}\s+", text.strip())[1:]

updated_count = 0
new_count = 0

new_rules = []

for section in rule_sections:
    try:
        lines = section.strip().splitlines()
        # Prefer the section heading itself as the rule name.
        rule_name = _heading_text(lines[0])

        # Fallback: if the heading is generic or empty, look for an explicit label inside the section.
        if not rule_name or rule_name.lower() in {"rule name", "rule-name"}:
            rn_match = re.search(r"\*\*Rule Name:\*\*\s*(.+)", section)
            if rn_match:
                rule_name = rn_match.group(1).strip()

        # Extract Rule Purpose
        purpose_match = re.search(r"\*\*Rule Purpose:\*\*\s*\n?(.*?)(?=\n\*\*Rule Spec|\n\*\*Specification|\n\*\*Code Block|\n\*\*Example|$)", section, re.DOTALL)
        rule_purpose = purpose_match.group(1).strip() if purpose_match else ""

        # Extract Rule Spec
        spec_match = re.search(r"\*\*Rule Spec:\*\*|\*\*Specification:\*\*", section)
        if spec_match:
            start = spec_match.end()
            next_marker = re.search(
                r"\n\*\*(Code Block|Example):\*\*|\n(?:\*{0,2}\s*)?DMN\s*:\s*(?:\*{0,2})?", section[start:], re.DOTALL | re.IGNORECASE
            )
            end = next_marker.start() + start if next_marker else len(section)
            rule_spec = section[start:end].strip()
        else:
            rule_spec = ""

        # Extract Code Block from any fenced code block (e.g., ```javascript, ```xml, ```apex, ```sql, or no language)
        code_match = re.search(r"```[a-zA-Z]*\n(.*?)```", section, re.DOTALL)
        code_block = code_match.group(1).strip() if code_match else ""

        # Extract Example
        example_match = re.search(
            r"\*\*Example:\*\*\s*\n?(.*?)(?=\n(?:\*{0,2}\s*)?DMN\s*:\s*(?:\*{0,2})?|\n## |\Z)",
            section,
            re.DOTALL | re.IGNORECASE,
        )
        example = example_match.group(1).strip() if example_match else ""
        # Safety: strip any embedded DMN marker from example if present
        if example:
            example = re.split(r"\n(?:\*{0,2}\s*)?DMN\s*:\s*(?:\*{0,2})?", example, flags=re.IGNORECASE)[0].strip()

        # Extract DMN block (now parses hit policy, inputs, outputs, and table)
        dmn_hit_policy = ""
        dmn_inputs = []
        dmn_outputs = []
        dmn_table = ""

        dmn_match = re.search(
            r"(?:^|\n)(?:\*{0,2}\s*)?DMN\s*:\s*\n?(.*?)(?=\n## |\Z)",
            section,
            re.DOTALL | re.IGNORECASE,
        )
        if dmn_match:
            raw_dmn = dmn_match.group(1).strip()
            # If DMN is in a fenced code block, extract the inner content
            m_code = re.search(r"```.*?\n(.*?)```", raw_dmn, re.DOTALL)
            dmn_body = m_code.group(1).strip() if m_code else raw_dmn
            # Remove markdown artifacts: backticks and bold markers
            dmn_body = re.sub(r"`+", "", dmn_body)
            dmn_body = re.sub(r"\*\*", "", dmn_body)

            # Hit Policy
            m_hp = re.search(r"Hit\s*Policy\s*:\s*([A-Za-z_]+)", dmn_body, re.IGNORECASE)
            if m_hp:
                dmn_hit_policy = m_hp.group(1).strip()

            # Inputs section (bulleted "- name: type" or "* name: type", accepts optional backticks)
            m_inputs = re.search(r"Inputs\s*:\s*\n(?P<block>(?:\s*[-*]\s*.*(?:\n|$))+)", dmn_body, re.IGNORECASE)
            if m_inputs:
                for ln in m_inputs.group("block").splitlines():
                    ln = ln.strip()
                    if not (ln.startswith("-") or ln.startswith("*")):
                        continue
                    ln = ln.lstrip("-*").strip()
                    if ":" in ln:
                        name, typ = ln.split(":", 1)
                        name = name.strip().strip('`')
                        typ = typ.strip().strip('`')
                        dmn_inputs.append({"name": name, "type": typ})
                    else:
                        field = ln.strip().strip('`')
                        dmn_inputs.append({"name": field, "type": ""})

            # Outputs section (bulleted "- name: type" or "* name: type", accepts optional backticks)
            m_outputs = re.search(r"Outputs\s*:\s*\n(?P<block>(?:\s*[-*]\s*.*(?:\n|$))+)", dmn_body, re.IGNORECASE)
            if m_outputs:
                for ln in m_outputs.group("block").splitlines():
                    ln = ln.strip()
                    if not (ln.startswith("-") or ln.startswith("*")):
                        continue
                    ln = ln.lstrip("-*").strip()
                    if ":" in ln:
                        name, typ = ln.split(":", 1)
                        name = name.strip().strip('`')
                        typ = typ.strip().strip('`')
                        dmn_outputs.append({"name": name, "type": typ})
                    else:
                        field = ln.strip().strip('`')
                        dmn_outputs.append({"name": field, "type": ""})

            # Decision table (contiguous block of lines containing '|' or divider rows)
            lines = [ln.rstrip() for ln in dmn_body.splitlines()]
            table_lines = []
            in_table = False
            for ln in lines:
                if ("|" in ln) or ("+" in ln) or re.search(r"-{2,}", ln):
                    table_lines.append(ln.rstrip())
                    in_table = True
                else:
                    if in_table:
                        break
            dmn_table = "\n".join(table_lines).strip()

        # Skip if rule with same name and timestamp already exists in seen
        k = _dedupe_key(rule_name, file_timestamp)
        if k in seen:
            continue

        existing = existing_by_name.get(rule_name)
        if existing:
            old_ts = existing.get("timestamp")
            if old_ts and old_ts >= file_timestamp:
                continue  # Skip older or same
            rule_id = existing.get("id")
            updated_count += 1
        else:
            rule_id = str(uuid.uuid4())
            new_count += 1

        seen.add(k)

        # Extract code file path (from either the "**Code Block:** <path>" inline form, or a subsequent "File: <path>" line)
        code_file = ""
        code_lines = None

        # 1) Try inline form on the same line as **Code Block:**
        m_codefile_inline = re.search(r"\*\*Code\s*Block:\*\*\s*`?([^`\n]+)`?", section, re.IGNORECASE)
        if m_codefile_inline:
            code_file = m_codefile_inline.group(1).strip()
        else:
            # 2) Try a following line that starts with "File: <path>" (common in newer docs)
            m_codefile_fileline = re.search(r"(?mi)^\s*File:\s*`?([^`\n]+)`?", section)
            if m_codefile_fileline:
                code_file = m_codefile_fileline.group(1).strip()

        # Final trim & backtick cleanup if anything slipped through
        if code_file:
            code_file = code_file.replace("`", "").strip()

        # Support formats like:
        #   "Line: 68-70" or "Lines: 68-70" (hyphen, en dash, or em dash)
        #   "Line: 68" (single line)
        #   case-insensitive, optional colon
        m_codelines = re.search(r"\bLine(?:s)?\s*:??\s*(\d+)(?:\s*[\-\u2013\u2014]\s*(\d+))?", section, re.IGNORECASE)
        if m_codelines:
            try:
                start_line = int(m_codelines.group(1))
                end_line = m_codelines.group(2)
                if end_line is not None:
                    end_line = int(end_line)
                else:
                    end_line = start_line
                code_lines = [start_line, end_line]
            except Exception:
                code_lines = None

        new_rules.append({
            "rule_name": rule_name,
            "rule_purpose": rule_purpose,
            "rule_spec": rule_spec,
            "code_block": code_block,
            "code_file": code_file,
            "code_lines": code_lines,
            "example": example,
            "dmn_hit_policy": dmn_hit_policy,
            "dmn_inputs": dmn_inputs,
            "dmn_outputs": dmn_outputs,
            "dmn_table": dmn_table,
            "timestamp": file_timestamp,
            "id": rule_id
        })

    except Exception as e:
        print(f"Failed to parse a rule section:\n{section[:100]}...\nError: {e}")

final_rules = {r["rule_name"]: r for r in existing_rules}
for r in new_rules:
    final_rules[r["rule_name"]] = r  # overwrite with latest

# Persist rules first to ensure output file exists (helps first-run linkage)
final_rules_list = list(final_rules.values())
os.makedirs(os.path.dirname(output_file), exist_ok=True)
with open(output_file, "w", encoding="utf-8") as out_file:
    json.dump(final_rules_list, out_file, indent=2)

print(
    f"Extracted {len(new_rules)} rules: {new_count} new, {updated_count} updated. "
    f"Total rules now: {len(final_rules_list)}. Saved to {output_file}"
)

# Build auxiliary model indices from PCPT headers and link this run to its rule IDs
_current_run_rule_ids = [r.get("id") for r in new_rules if r.get("id")]
write_model_sources_and_runs(rule_ids_for_output=_current_run_rule_ids, output_file_path=input_file)