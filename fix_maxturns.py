#!/usr/bin/env python3
"""Fix the --max-turns injection in orca_executor.py."""
import re

with open('~/school-core/orca_executor.py', 'r') as f:
    content = f.read()

# 1. Insert max_turns computation before launcher.write_text
# 2. Remove the broken lines inside the bash string
# 3. Fix the merged hermes chat line

# Replace the entire broken launcher.write_text block
old_block = '''        launcher.write_text(
            "#!/usr/bin/env bash\\n"
            f'cd {shlex.quote(str(wp))}\\n'
            f'# Difficulty-aware turn cap: medium tasks need multiple iterations
            # to write code (not just reconnaissance)
            _TURNS = {"easy": 1, "medium": 5, "hard": 8, "diploma": 10}
            max_turns = _TURNS.get(difficulty, 1)
            f'hermes chat -q "$(cat {shlex.quote(str(task_file))})" '            f"--yolo --quiet --max-turns {max_turns} "'''

new_block = '''        # Difficulty-aware turn cap: medium tasks need multiple iterations
        # to write code (not just reconnaissance)
        _TURNS = {"easy": 1, "medium": 5, "hard": 8, "diploma": 10}
        max_turns = _TURNS.get(difficulty, 1)
        launcher.write_text(
            "#!/usr/bin/env bash\\n"
            f'cd {shlex.quote(str(wp))}\\n'
            f'hermes chat -q "$(cat {shlex.quote(str(task_file))})" '
            f"--yolo --quiet --max-turns {max_turns} "\\"'''

if old_block in content:
    content = content.replace(old_block, new_block)
    with open('~/school-core/orca_executor.py', 'w') as f:
        f.write(content)
    print("Fixed!")
else:
    print("Old block not found — checking current state...")
    # Try to find what's actually there
    idx = content.find('launcher.write_text(')
    if idx >= 0:
        print(content[idx:idx+500])
