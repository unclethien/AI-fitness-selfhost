"""Tests for the sidecar loader's credential handling.

Narrow on purpose: the rest of `load.py` is verified by running it against the real
spreadsheet and a real wger, which no fake reproduces usefully. What is worth testing in
isolation is the token guard, because getting it wrong fails a setup step for a reason
that looks nothing like the cause — a 401 from an endpoint that does not require auth.

Run: python sidecar/test_load.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load import usable_token  # noqa: E402

failures: list[str] = []


def check(label, cond, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {extra}")


print("\ntoken guard")

check("a real token is used", usable_token("73f1ee4fbc4f58bfcd") == "73f1ee4fbc4f58bfcd")
check("surrounding whitespace is stripped",
      usable_token("  73f1ee4f  ") == "73f1ee4f")

# The deployment ships this exact value until the wger account exists.
check("the shipped placeholder is refused",
      usable_token("CHANGEME_wger_token") is None)
check("placeholder detection is case-insensitive",
      usable_token("changeme_wger_token") is None)
check("a placeholder with whitespace is refused",
      usable_token("\n CHANGEME_wger_token \n") is None)

check("None stays None", usable_token(None) is None)
check("empty is None", usable_token("") is None)
check("whitespace-only is None", usable_token("   ") is None)

# A token that merely mentions the word must still work — the guard keys on the prefix
# the deployment actually writes, not on the substring appearing anywhere.
check("a real token containing 'changeme' later is kept",
      usable_token("abc123changeme") == "abc123changeme")

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all loader tests passed")
