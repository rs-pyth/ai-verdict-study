"""
Validate the four stored fallback verdicts in config.FALLBACK_VERDICTS before
launch.  Run with:  python check_fallbacks.py   (exit code 0 = all good)

Each fallback must be plain text, 80-100 words, start with the exact verdict
sentence for its cell's direction, and differ from the others.
"""
import sys

import config
import verdicts

problems = verdicts.validate_fallbacks(
    config.FALLBACK_VERDICTS, config.TARGET_VERDICT, config.VERDICT_WORD_MIN, config.VERDICT_WORD_MAX)

for (stance, condition), text in sorted(config.FALLBACK_VERDICTS.items()):
    n = len(text.split()) if text else 0
    print(f"  {stance:3} / {condition:8} -> {n:3} words  {'(missing)' if not text else ''}")
if problems:
    print("\nNOT READY:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("\nAll four fallbacks are present and valid.")
