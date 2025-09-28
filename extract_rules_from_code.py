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
EXECUTIONS_JSON = ".model/executions.json"
ARTIFACTS_JSON = ".model/artifacts.json"

# Log parsing patterns
RE_OUTPUT_REPORT = re.compile(r"^\s*Output report:\s*(?P<path>.+)\s*$", re.IGNORECASE)
RE_OUTPUT_ALSO  = re.compile(r"^\s*Output file created at:\s*(?P<path>.+)\s*$", re.IGNORECASE)
RE_FILE_HEADER  = re.compile(r"^\s*File:\s*(?P<path>.+?)\s*$")
RE_FENCE_START  = re.compile(r"^\s*```")
RE_REPORT_SAVED = re.compile(r"^\s*(Report saved|Saved report|Saving report):\s*(?P<path>.+)\s*$", re.IGNORECASE)
RE_OUTPUT_GENERIC = re.compile(r"^\s*(Output|Wrote|Saved):\s*(?P<path>.+)\s*$", re.IGNORECASE)

# Accept input file path from command line argument
if len(sys.argv) < 2:
    print("Usage: python extract_rules.py <input_file>")
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

def _load_json_file(path: str, default):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def _save_json_file(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def _all_logs() -> list:
    patterns = ["**/*.log", "**/*.txt", "**/*.out"]
    results = []
    for pat in patterns:
        results.extend(glob.glob(os.path.join(LOG_DIR, pat), recursive=True))
    return sorted(results)

def _log_mentions_report(log_path: str, report_path: str) -> bool:
    # Normalize comparisons: absolute path, lowercase, and basename fallback
    try:
        report_abs = os.path.abspath(report_path)
    except Exception:
        report_abs = report_path
    report_abs_l = report_abs.lower()
    report_base = os.path.basename(report_abs)
    report_base_l = report_base.lower()

    patterns = [RE_OUTPUT_REPORT, RE_OUTPUT_ALSO, RE_REPORT_SAVED, RE_OUTPUT_GENERIC]
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_l = line.lower()
                # direct containment check first
                if report_abs_l in line_l or report_base_l in line_l:
                    return True
                # structured markers
                for rx in patterns:
                    m = rx.search(line)
                    if m:
                        mentioned = (m.group("path") or "").strip()
                        mentioned_abs = os.path.abspath(mentioned)
                        if mentioned_abs.lower() == report_abs_l or os.path.basename(mentioned_abs).lower() == report_base_l:
                            return True
    except FileNotFoundError:
        return False
    return False

def _find_matching_log_for_report(report_path: str) -> Optional[str]:
    candidates = [p for p in _all_logs() if _log_mentions_report(p, report_path)]
    if candidates:
        return candidates[-1]
    # fallback by time proximity
    return _fallback_log_by_time(report_path)

def _fallback_log_by_time(report_path: str) -> Optional[str]:
    """If no explicit mention is found, choose the newest log within +/- 2 hours of the report's mtime."""
    try:
        target_ts = os.path.getmtime(report_path)
    except Exception:
        return None
    window = 2 * 3600  # 2 hours
    candidates = []
    for p in _all_logs():
        try:
            t = os.path.getmtime(p)
            if abs(t - target_ts) <= window:
                candidates.append((t, p))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]

def _norm_code_block(s: str) -> str:
    # normalize whitespace for robust matching
    return "\n".join([ln.rstrip() for ln in (s or "").strip().splitlines()]).strip()

def _parse_log_for_artifacts_and_codeblocks(log_path: str) -> Tuple[List[str], Dict[str, str]]:
    """
    Parse the log to collect artifacts and a mapping of normalized code_block -> artifact_path.
    Returns (artifacts_list, codehash_to_artifact map using normalized text).
    """
    artifacts: List[str] = []
    code_to_artifact: Dict[str, str] = {}

    current_artifact: Optional[str] = None
    in_fence = False
    buf: List[str] = []

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.rstrip("\n")

            m_file = RE_FILE_HEADER.match(line)
            if m_file and not in_fence:
                current_artifact = m_file.group("path").strip()
                if current_artifact not in artifacts:
                    artifacts.append(current_artifact)
                continue

            if RE_FENCE_START.match(line):
                in_fence = not in_fence
                if not in_fence and buf and current_artifact:
                    code_norm = _norm_code_block("\n".join(buf))
                    if code_norm:
                        code_to_artifact.setdefault(code_norm, current_artifact)
                    buf = []
                continue

            if in_fence:
                buf.append(line)

    return artifacts, code_to_artifact

def _heading_text(line: str) -> str:
    """Extract clean heading text from a markdown heading line.
    Removes leading/trailing '#' and surrounding whitespace/markers.
    """
    s = (line or "").strip()
    # remove leading hashes and spaces
    s = re.sub(r"^\s*#{1,6}\s*", "", s)
    # remove trailing hashes and spaces
    s = re.sub(r"\s*#{1,6}\s*$", "", s)
    return s.strip(" *-\t")

def _ensure_execution_record(executions: List[dict], log_path: str, report_path: str,
                             artifacts: list[str], rule_ids: list[str]) -> str:
    # Normalize to absolute paths for robust matching
    log_abs = os.path.abspath(log_path) if log_path else log_path

    # If an execution already exists for this log path, reuse it and merge fields
    for e in executions:
        try:
            existing_log_abs = os.path.abspath(e.get("log_path", ""))
        except Exception:
            existing_log_abs = e.get("log_path")
        if existing_log_abs and log_abs and existing_log_abs == log_abs:
            # Update report path if provided
            if report_path and e.get("output_report_path") != report_path:
                e["output_report_path"] = report_path
            # Merge artifacts (dedupe)
            existing_artifacts = e.get("artifacts", []) or []
            merged_artifacts = sorted(set(existing_artifacts) | set(artifacts or []))
            e["artifacts"] = merged_artifacts
            # Merge rule IDs (dedupe)
            existing_rule_ids = e.get("rule_ids", []) or []
            merged_rule_ids = sorted(set(existing_rule_ids) | set(rule_ids or []))
            e["rule_ids"] = merged_rule_ids
            return e.get("id")

    # Otherwise create a new execution (no previous match)
    exec_id = str(uuid.uuid4())
    executions.append({
        "id": exec_id,
        "created_at": _now_iso(),
        "log_path": log_path,
        "output_report_path": report_path,
        "artifacts": artifacts,
        "rule_ids": rule_ids
    })
    return exec_id

def _merge_artifacts(artifacts_json: Dict[str, dict], artifacts: List[str], execution_id: str) -> Dict[str, dict]:
    now = _now_iso()
    for a in artifacts:
        entry = artifacts_json.get(a)
        if not entry:
            artifacts_json[a] = {
                "id": a,
                "first_seen": now,
                "last_seen": now,
                "seen_in_executions": [execution_id]
            }
        else:
            entry["last_seen"] = now
            if execution_id not in entry.get("seen_in_executions", []):
                entry.setdefault("seen_in_executions", []).append(execution_id)
    return artifacts_json

def link_execution_and_artifacts(report_path: str, rules_list: List[Dict]) -> Tuple[Optional[str], int, int]:
    """
    Locate the producing execution log, create/update executions.json & artifacts.json,
    and annotate each rule in-place with `execution_id` and `artifact_path` (when matched).
    Returns (execution_id or None, num_rules_annotated, num_artifacts_found).
    """
    log_path = _find_matching_log_for_report(report_path)
    if not log_path:
        if os.environ.get("PCPT_DEBUG"):
            print(f"[DEBUG] No matching log found in {LOG_DIR}. Consider setting PCPT_LOG_DIR or PCPT_DEBUG=1.")
            print(f"[DEBUG] Checked {len(_all_logs())} log files. Report: {os.path.abspath(report_path)}")
        return None, 0, 0

    artifacts, code_to_artifact = _parse_log_for_artifacts_and_codeblocks(log_path)

    executions = _load_json_file(EXECUTIONS_JSON, default=[])
    artifacts_json = _load_json_file(ARTIFACTS_JSON, default={})

    rule_ids = [r.get("id") for r in rules_list if r.get("id")]
    exec_id = _ensure_execution_record(executions, log_path, report_path, artifacts, rule_ids)

    artifacts_json = _merge_artifacts(artifacts_json, artifacts, exec_id)

    matched = 0
    for r in rules_list:
        r["execution_id"] = exec_id
        code_norm = _norm_code_block(r.get("code_block", ""))
        art = code_to_artifact.get(code_norm)
        if art:
            r["artifact_path"] = art
            matched += 1

    _save_json_file(EXECUTIONS_JSON, executions)
    _save_json_file(ARTIFACTS_JSON, artifacts_json)

    return exec_id, matched, len(artifacts)

output_file = ".model/code_business_rules.json"

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

# Normalize rule section headers
text = re.sub(r"###\s*\d+\.\s*\*\*Rule Name:\*\*\s*", "## ", text)  # Format 2
text = re.sub(r"##\s*Rule Name:\s*", "## ", text)                   # Format 3
text = re.sub(r"\n---+\n", "\n", text)                              # Remove separators

# Split into rule sections
rule_sections = re.split(r"(?m)^\s{0,3}#{1,6}\s+", text.strip())[1:]

updated_count = 0

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
            next_marker = re.search(r"\n\*\*(Code Block|Example):\*\*", section[start:], re.DOTALL)
            end = next_marker.start() + start if next_marker else len(section)
            rule_spec = section[start:end].strip()
        else:
            rule_spec = ""

        # Extract Code Block
        code_match = re.search(r"```sql\n(.*?)```", section, re.DOTALL)
        code_block = code_match.group(1).strip() if code_match else ""

        # Extract Example
        example_match = re.search(r"\*\*Example:\*\*\s*\n?(.*?)(?=\n## |\Z)", section, re.DOTALL)
        example = example_match.group(1).strip() if example_match else ""

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

        seen.add(k)

        new_rules.append({
            "rule_name": rule_name,
            "rule_purpose": rule_purpose,
            "rule_spec": rule_spec,
            "code_block": code_block,
            "example": example,
            "timestamp": file_timestamp,
            "id": rule_id
        })

    except Exception as e:
        print(f"Failed to parse a rule section:\n{section[:100]}...\nError: {e}")

final_rules = {r["rule_name"]: r for r in existing_rules}
for r in new_rules:
    final_rules[r["rule_name"]] = r  # overwrite with latest

# Annotate with execution + artifacts, then persist
final_rules_list = list(final_rules.values())
exec_id, matched_rules, artifact_count = link_execution_and_artifacts(input_file, final_rules_list)

os.makedirs(os.path.dirname(output_file), exist_ok=True)

with open(output_file, "w", encoding="utf-8") as out_file:
    json.dump(final_rules_list, out_file, indent=2)

print(f"Extracted {len(new_rules)} new rules. Total rules now: {len(final_rules_list)}. Saved to {output_file}")
print(f"Updated {updated_count} existing rules with newer content.")
if exec_id:
    print(f"Linked to execution: {exec_id} (artifacts: {artifact_count}, rules annotated with artifact_path: {matched_rules})")
else:
    print("No matching execution log found in ~/.pcpt/log; rules saved without execution linkage.")