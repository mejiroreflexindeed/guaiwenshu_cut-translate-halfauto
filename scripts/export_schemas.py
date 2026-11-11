"""Export every v2 record kind to schemas/<Kind>.schema.json (plan 1.1, section 4.1).

contracts.py is the single source of truth; this script only renders it. Run from any directory:
    python scripts/export_schemas.py [--check]
"""
from pathlib import Path
import argparse
import json
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "app/tools"))
from upgrade.contracts import RECORD_KINDS, to_json_schema  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--check", action="store_true", help="exit 1 when schemas/ differs from contracts.py")
args = parser.parse_args()
target = root / "schemas"
target.mkdir(exist_ok=True)
changed = []
for kind in RECORD_KINDS:
    text = json.dumps(to_json_schema(kind), ensure_ascii=False, indent=2) + "\n"
    path = target / f"{kind}.schema.json"
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        changed.append(path.name)
        if not args.check:
            path.write_text(text, encoding="utf-8", newline="\n")
index = {"schema_version": 2, "kinds": list(RECORD_KINDS)}
index_text = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
index_path = target / "index.json"
if not index_path.is_file() or index_path.read_text(encoding="utf-8") != index_text:
    changed.append(index_path.name)
    if not args.check:
        index_path.write_text(index_text, encoding="utf-8", newline="\n")
print(("would change" if args.check else "wrote") + f" {len(changed)} file(s) in {target}")
raise SystemExit(1 if (args.check and changed) else 0)
