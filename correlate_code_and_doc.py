import json
import faiss
from sentence_transformers import SentenceTransformer
from sklearn.preprocessing import normalize
import numpy as np
import warnings
from urllib3.exceptions import NotOpenSSLWarning
import uuid
from copy import deepcopy

warnings.filterwarnings("ignore", category=NotOpenSSLWarning)

# Input file paths
code_file = ".model/code_business_rules.json"
doc_file = ".model/documented_business_rules.json"
output_file = ".model/business_rules.json"

# Load rules
with open(code_file, "r", encoding="utf-8") as f:
    code_rules = json.load(f)


with open(doc_file, "r", encoding="utf-8") as f:
    document_rules = json.load(f)

# Extract rule names
code_names = [r["rule_name"].strip() for r in code_rules]
doc_names = [r["rule_name"].strip() for r in document_rules]

# Embedding model
model = SentenceTransformer("all-MiniLM-L6-v2")

# Embed document rule names
doc_embeddings = model.encode(doc_names, convert_to_numpy=True)
doc_embeddings = normalize(doc_embeddings)

# Build FAISS index
dim = doc_embeddings.shape[1]
index = faiss.IndexFlatIP(dim)
index.add(doc_embeddings)

used_doc_indices = set()
merged = []
SIMILARITY_THRESHOLD = 0.6

for i, code_rule in enumerate(code_rules):
    code_vec = model.encode([code_rule["rule_name"]])
    code_vec = normalize(code_vec)

    # Search FAISS
    D, I = index.search(code_vec, k=len(document_rules))
    
    match_found = False
    for j in range(len(I[0])):
        doc_idx = I[0][j]
        similarity = D[0][j]

        if doc_idx not in used_doc_indices and similarity >= SIMILARITY_THRESHOLD:
            doc_rule = document_rules[doc_idx]
            used_doc_indices.add(doc_idx)
            match_found = True
            print(f"✅ Matched '{code_rule['rule_name']}' → '{doc_rule['rule_name']}' (score: {similarity:.2f})")

            # Start with all fields from the code rule to preserve extras (e.g., execution_id, artifact_path)
            merged_rule = dict(code_rule)
            # Overlay doc-derived identity/metadata
            merged_rule.update({
                "rule_id": doc_rule.get("rule_id"),
                "rule_name": doc_rule.get("rule_name", merged_rule.get("rule_name")),
                "rule_category": doc_rule.get("rule_category"),
                "business_area": doc_rule.get("business_area"),
                "owner": doc_rule.get("owner"),
                "match_score": round(float(similarity), 4)
            })
            merged.append(merged_rule)
            break

    if not match_found:
        print(f"⚠️ No good match for '{code_rule['rule_name']}' — adding unmatched.")
        # Preserve the full code rule object and mark unmatched metadata
        merged_rule = dict(code_rule)
        merged_rule.update({
            "rule_id": None,
            "rule_category": None,
            "business_area": None,
            "owner": None,
            "match_score": 0.0
        })
        merged.append(merged_rule)

# Save output
with open(output_file, "w", encoding="utf-8") as f:
    json.dump(merged, f, indent=2)

print(f"\n✅ Semantic match complete. Saved {len(merged)} merged rules to {output_file}")