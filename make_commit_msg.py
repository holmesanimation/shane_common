"""
Reads docs/devlog/current.md and prints a commit message to copy-paste into SourceTree.
Covers ALL entries in current.md (i.e. everything since the last commit).
Usage: python make_commit_msg.py
"""

import re
from pathlib import Path

DEVLOG = Path(__file__).parent / "docs" / "devlog" / "current.md"

text = DEVLOG.read_text(encoding="utf-8")

# Split into entry blocks on ## headings (skip the # title line)
entry_pattern = re.compile(r"^## (.+)$", re.MULTILINE)
entry_headings = list(entry_pattern.finditer(text))

if not entry_headings:
    print("No devlog entries found in current.md")
    raise SystemExit(1)

titles = []
all_files = []

for i, match in enumerate(entry_headings):
    titles.append(match.group(1).strip())
    # Slice text for this entry
    start = match.start()
    end = entry_headings[i + 1].start() if i + 1 < len(entry_headings) else len(text)
    block = text[start:end]

    files_match = re.search(
        r"^### Files touched\n(.*?)(?=^###|\Z)", block, re.MULTILINE | re.DOTALL
    )
    if files_match:
        for line in files_match.group(1).splitlines():
            line = line.strip()
            if line.startswith("-"):
                clean = re.sub(r"`([^`]+)`", r"\1", line[1:].strip())
                path_part = clean.split(" \u2014")[0].split(" -")[0].strip()
                if path_part and path_part not in all_files:
                    all_files.append(path_part)

print("=" * 60)
if len(titles) == 1:
    print(titles[0])
else:
    # Multiple entries: use a combined summary line + list each title
    date_prefix = titles[0].split(" \u2014")[0]
    print(f"{date_prefix} + {len(titles) - 1} more")
    print()
    for t in titles:
        print(f"  {t}")
if all_files:
    print()
    for f in all_files:
        print(f"  {f}")
print("=" * 60)
