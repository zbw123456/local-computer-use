"""Check executable/JSONL behavior without requesting permissions or touching UI."""
import json
import subprocess
from pathlib import Path

binary = Path(__file__).resolve().parent.parent / ".local/computer-use-helper"
requests = [{"op": "doctor"}, {"op": "unsupported"}, {"op": "act", "generation": "stale"}]
result = subprocess.run([str(binary)], input="".join(json.dumps(r) + "\n" for r in requests),
                        text=True, capture_output=True, check=True, timeout=10)
rows = [json.loads(line) for line in result.stdout.splitlines()]
assert len(rows) == 3, "Expected one response per request"
assert rows[0]["ok"] and type(rows[0]["result"]["accessibility"]) is bool
assert rows[0]["result"]["platform"] == "macOS"
assert not rows[1]["ok"] and not rows[2]["ok"]
print("Native helper: doctor, invalid operation and stale action checks passed.")
