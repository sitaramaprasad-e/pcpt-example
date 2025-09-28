#!/usr/bin/env python3
# categorize_rules.py
import os
import json
import shutil
import subprocess
from typing import Any, Dict, List, Optional

# ----------------------------
# Configuration
# ----------------------------
RULE_CATEGORIES_JSON = ".model/rule_categories.json"
BUSINESS_RULES_JSON = ".model/business_rules.json"

TMP_DIR = ".tmp/rule_categorization"
DYNAMIC_RULE_FILE = os.path.join(TMP_DIR, "rule.json")

# Per your exact invocation shape:
OUTPUT_DIR_ARG = "docs"
OUTPUT_FILE_ARG = "categorise-rule/categorise-rule.md"
OUTPUT_ABS_PATH = os.path.join(OUTPUT_DIR_ARG, OUTPUT_FILE_ARG)

PROMPT_NAME = "categorise-rule.templ"

# ----------------------------
# Utilities
# ----------------------------
def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def dump_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def ensure_paths() -> None:
    os.makedirs(TMP_DIR, exist_ok=True)
    # Make sure rule_categories.json exists
    if not os.path.exists(RULE_CATEGORIES_JSON):
        raise FileNotFoundError(f"Missing {RULE_CATEGORIES_JSON}")
    # Make sure business_rules.json exists
    if not os.path.exists(BUSINESS_RULES_JSON):
        raise FileNotFoundError(f"Missing {BUSINESS_RULES_JSON}")

    # Stage rule categories into TMP so both inputs live under a single mount point
    categories_tmp_path = os.path.join(TMP_DIR, "rule_categories.json")
    try:
        shutil.copyfile(RULE_CATEGORIES_JSON, categories_tmp_path)
    except Exception as e:
        raise RuntimeError(f"Failed to stage rule categories into {TMP_DIR}: {e}")

def is_missing_category(rule: Dict[str, Any]) -> bool:
    cat = rule.get("rule_category")
    return cat is None or (isinstance(cat, str) and cat.strip() == "")

def run_pcpt_for_rule(dynamic_rule_file: str) -> Optional[Dict[str, Any]]:
    """
    Calls:
    pcpt.sh run-custom-prompt \
      --input-file .tmp/rule_categorization/rule_categories.json \
      --input-file2 .tmp/rule_categorization/rule.json \
      --output docs existing_docs/business_rule.json \
      categorise-rule.templ
    Returns parsed JSON from OUTPUT_ABS_PATH (expects selectedCategory).
    """
    # Remove previous output to avoid reading stale results
    if os.path.exists(OUTPUT_ABS_PATH):
        try:
            os.remove(OUTPUT_ABS_PATH)
        except OSError:
            pass

    categories_tmp_path = os.path.join(TMP_DIR, "rule_categories.json")

    # Ensure the output directory (including any nested subfolders) exists on host
    out_parent = os.path.join(OUTPUT_DIR_ARG, os.path.dirname(OUTPUT_FILE_ARG))
    if os.path.dirname(OUTPUT_FILE_ARG):
        os.makedirs(out_parent, exist_ok=True)
    else:
        os.makedirs(OUTPUT_DIR_ARG, exist_ok=True)

    cmd = [
        "pcpt.sh",
        "run-custom-prompt",
        "--input-file",
        categories_tmp_path,      # staged in TMP_DIR
        "--input-file2",
        dynamic_rule_file,        # also in TMP_DIR
        "--output",
        OUTPUT_DIR_ARG,
        DYNAMIC_RULE_FILE,
        PROMPT_NAME,
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ pcpt.sh failed: {e}")
        return None

    if not os.path.exists(OUTPUT_ABS_PATH):
        print("⚠️ Expected output not found at:", OUTPUT_ABS_PATH)
        return None

    try:
        return load_json(OUTPUT_ABS_PATH)
    except Exception as e:
        print(f"⚠️ Failed to parse JSON from {OUTPUT_ABS_PATH}: {e}")
        return None

# ----------------------------
# Main
# ----------------------------
def main() -> None:
    ensure_paths()

    # Backup current business rules (for safety)
    backup_path = f"{BUSINESS_RULES_JSON}.bak"
    try:
        shutil.copyfile(BUSINESS_RULES_JSON, backup_path)
        print(f"🧷 Backup created: {backup_path}")
    except Exception as e:
        print(f"⚠️ Could not create backup ({backup_path}): {e}")

    rules: List[Dict[str, Any]] = load_json(BUSINESS_RULES_JSON)
    if not isinstance(rules, list):
        raise ValueError(f"{BUSINESS_RULES_JSON} must be a JSON array")

    total = len(rules)
    skipped = 0
    categorized = 0
    unchanged_with_category = 0

    for idx, rule in enumerate(rules):
        if not is_missing_category(rule):
            # Already has a category — leave it as-is
            unchanged_with_category += 1
            continue

        # Build a one-item array as your "dynamically generated" rule.json
        single_rule_payload = [rule]

        # Write dynamic file
        dump_json(DYNAMIC_RULE_FILE, single_rule_payload)

        # Run pcpt.sh on that dynamic file
        result = run_pcpt_for_rule(DYNAMIC_RULE_FILE)
        if not result or "selectedCategory" not in result:
            print(f"⚠️ No selectedCategory returned for rule '{rule.get('rule_name','(unnamed)')}'. Skipping.")
            skipped += 1
            continue

        selected = result["selectedCategory"]
        name = selected.get("name")
        explanation = selected.get("explanation")

        if not name:
            print(f"⚠️ selectedCategory missing 'name' for rule '{rule.get('rule_name','(unnamed)')}'. Skipping.")
            skipped += 1
            continue

        # Update rule fields in-place
        rule["rule_category"] = name
        # Only add the two fields you requested:
        rule["ai_categorized"] = True
        rule["category_explanation"] = explanation or ""

        categorized += 1
        print(f"✅ Categorized: {rule.get('rule_name','(unnamed)')} → {name}")

    # Persist changes
    dump_json(BUSINESS_RULES_JSON, rules)

    print("\n──────── Summary ────────")
    print(f"Total rules:                  {total}")
    print(f"Already had category:         {unchanged_with_category}")
    print(f"Newly AI-categorized:         {categorized}")
    print(f"Skipped (no/invalid output):  {skipped}")
    print(f"📄 Updated file: {BUSINESS_RULES_JSON}")

if __name__ == "__main__":
    main()