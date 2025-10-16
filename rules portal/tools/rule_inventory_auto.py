import pandas as pd
import json
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from datetime import datetime
import hashlib
import os

# -----------------------
# CONFIG
JSON_FILE = "rules.json"
EXCEL_FILE = "rules_inventory.xlsx"
# -----------------------

def compute_logic_hash(logic_text):
    """Generate hash for rule logic content."""
    return hashlib.md5(logic_text.strip().encode("utf-8")).hexdigest()

def generate_rule_id(existing_ids, source_system="SRC"):
    """Generate unique rule ID per source system and date."""
    today = datetime.now().strftime("%Y%m%d")
    prefix = source_system.upper()[:5]  # limit prefix length
    max_seq = 0
    for rid in existing_ids:
        if rid.startswith(f"{prefix}-{today}-"):
            try:
                seq = int(rid.split("-")[-1])
                max_seq = max(max_seq, seq)
            except:
                continue
    new_seq = max_seq + 1
    return f"{prefix}-{today}-{str(new_seq).zfill(4)}"

class JsonHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.src_path.endswith(JSON_FILE):
            return

        try:
            print("🔄 JSON change detected. Updating Excel...")

            # Load new JSON rules
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            df = pd.json_normalize(data)

            # Load existing Excel if exists
            if os.path.exists(EXCEL_FILE):
                existing_df = pd.read_excel(EXCEL_FILE)
                existing_ids = existing_df.get("Rule ID", []).tolist()
                existing_hashes = {}
                for idx, row in existing_df.iterrows():
                    logic = str(row.get("Extracted Expression", ""))
                    existing_hashes[compute_logic_hash(logic)] = row["Rule ID"]
            else:
                existing_df = pd.DataFrame()
                existing_ids = []
                existing_hashes = {}

            # Ensure columns
            for col in ["Rule ID", "Aliases", "Created At", "Last Updated", "Updated Flag"]:
                if col not in df.columns:
                    df[col] = ""

            # Process each new/updated rule
            new_rows = []
            for _, row in df.iterrows():
                logic_text = str(row.get("Extracted Expression", ""))
                logic_hash = compute_logic_hash(logic_text)
                rule_name = row.get("Rule Name", "")
                source = row.get("Source System", "SRC")

                if logic_hash in existing_hashes:
                    # Duplicate logic
                    rule_id = existing_hashes[logic_hash]
                    row["Rule ID"] = rule_id

                    # Handle alias updates
                    existing_row = existing_df.loc[existing_df["Rule ID"] == rule_id]
                    if not existing_row.empty:
                        existing_aliases = str(existing_row["Aliases"].values[0]).split(",") if pd.notna(existing_row["Aliases"].values[0]) else []
                        alias_list = set(a.strip() for a in existing_aliases)
                        alias_list.add(rule_name)
                        row["Aliases"] = ", ".join(sorted(alias_list))
                        row["Created At"] = existing_row["Created At"].values[0]
                        row["Last Updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        row["Updated Flag"] = "Yes"
                else:
                    # New unique rule
                    new_id = generate_rule_id(existing_ids, source_system=source)
                    existing_ids.append(new_id)
                    row["Rule ID"] = new_id
                    row["Aliases"] = rule_name
                    row["Created At"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    row["Last Updated"] = row["Created At"]
                    row["Updated Flag"] = "New"
                    existing_hashes[logic_hash] = new_id

                new_rows.append(row)

            # Combine and save updated inventory
            updated_df = pd.DataFrame(new_rows)
            updated_df = pd.concat([existing_df, updated_df], ignore_index=True)
            updated_df.drop_duplicates(subset=["Rule ID"], keep="last", inplace=True)
            updated_df.to_excel(EXCEL_FILE, index=False)

            print("✅ Excel updated successfully!")

        except Exception as e:
            print("❌ Error:", e)


if __name__ == "__main__":
    print(f"👀 Monitoring '{JSON_FILE}' for changes... (Press Ctrl+C to stop)")
    event_handler = JsonHandler()
    observer = Observer()
    observer.schedule(event_handler, ".", recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        observer.join()
