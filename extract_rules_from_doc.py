import re
import json

# Input and output file paths
input_filename = "existing_docs/business_rules_document/Claims_Processing_Business_Rules_v1.0.md"
output_filename = ".model/documented_business_rules.json"

# Define a regex pattern to extract the relevant fields from each rule block
rule_pattern = re.compile(
    r"### 📘 Rule ID: (BR-\d{3}) – (.*?)\n"
    r"- \*\*Category\*\*: (.*?)  \n"
    r"- \*\*Business Area\*\*: (.*?)  \n"
    r"- \*\*Rule Name\*\*: (.*?)  \n"
    r".*?"
    r"- \*\*Owner\*\*: (.*?)  \n",
    re.DOTALL
)

# Read the full text of the input document
with open(input_filename, "r", encoding="utf-8") as file:
    document = file.read()

# Use the regex pattern to extract matches
matches = rule_pattern.findall(document)

# Construct a list of rules with the required fields
rules = []
for match in matches:
    rule_id, rule_name, category, business_area, rule_name_repeat, owner = match
    rules.append({
        "rule_id": rule_id.strip(),
        "rule_name": rule_name.strip(),
        "rule_category": category.strip(),
        "business_area": business_area.strip(),
        "owner": owner.strip()
    })

# Write the list to a JSON file
with open(output_filename, "w", encoding="utf-8") as json_file:
    json.dump(rules, json_file, indent=2)

print(f"{len(rules)} rules extracted and written to {output_filename}")