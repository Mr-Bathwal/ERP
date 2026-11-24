"""Build the dashboard's data files from the repository.

Run from the repository root:

    .venv\\Scripts\\python.exe -I dashboard\\build_data.py

It writes `dashboard/policy.json` from the policy the agent actually runs on
(parsed by `procureflow.policy_rag.parse_policy`, not re-typed), and copies
the committed eval results and traces from `eval/results/` next to the page.
The page computes every number it shows from these files at load time.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent
ROOT = DASHBOARD.parent
# `-I` (isolated mode) leaves the script's directory, not the repo root, on
# sys.path, so the package is made importable here rather than by the caller.
sys.path.insert(0, str(ROOT))

from procureflow.approval import parse_authority  # noqa: E402
from procureflow.policy_rag import (  # noqa: E402
    POLICY_PATH,
    fingerprint,
    parse_policy,
    policy_version,
)

RESULTS = ROOT / "eval" / "results"
COPIES = ("results-v1.json", "results-v2.json", "results-v3.json", "traces.json")

_SUMMARY_ROW = re.compile(
    r"^\|\s*(R\d+)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|", re.MULTILINE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*])")
_LIST_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.*)$")
_BOLD_LEAD = re.compile(r"^\*\*([^*]+?)\.?\*\*")


def _plain(md: str) -> str:
    """Markdown to one line of text. Backticks are kept: the page renders
    them as code, because they are field names and exception codes."""
    text = md.replace("**", "")
    text = re.sub(r"(?<!\w)\*(?!\s)([^*]+?)\*(?!\w)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _effect(rule_text: str) -> str:
    """The rule's first sentence, which in this policy states its effect.

    Two rules (R8, R12) open with a sentence that ends in a colon and put the
    substance in a list; for those the list is folded into the sentence.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", rule_text) if p.strip()]
    prose = [p for p in paragraphs if not p.lstrip().startswith((">", "|"))]
    if not prose:
        return ""
    first = _plain(prose[0])
    sentence = _SENTENCE_END.split(first, maxsplit=1)[0]
    if not sentence.endswith(":"):
        return sentence
    following = prose[1] if len(prose) > 1 else ""
    items = []
    for line in following.splitlines():
        m = _LIST_ITEM.match(line)
        if not m:
            continue
        item = m.group(1).strip()
        lead = _BOLD_LEAD.match(item)
        item = lead.group(1).strip().lower() if lead else _plain(item)
        items.append(item.rstrip(";.").strip())
    return sentence + " " + "; ".join(items) + "." if items else sentence


def build_policy() -> dict:
    rules = parse_policy()
    text = POLICY_PATH.read_text(encoding="utf-8")
    summary = {m.group(1): (m.group(2).strip(), m.group(4).strip())
               for m in _SUMMARY_ROW.finditer(text)}
    out = []
    for r in rules:
        subject, on_breach = summary.get(r.rule_id, ("", ""))
        out.append({
            "id": r.rule_id,
            "title": r.title,
            "threshold": r.threshold,
            "effect": _effect(r.text),
            "subject": subject,
            "on_breach": on_breach,
        })
    approvers = [{"id": a.approver_id, "name": a.name, "role": a.role,
                  "limit_paise": a.limit_paise} for a in parse_authority(text)]
    return {
        "source": POLICY_PATH.relative_to(ROOT).as_posix(),
        "version": policy_version(),
        "sha256": fingerprint(rules),
        "rules": out,
        "approvers": approvers,
    }


def main() -> int:
    policy = build_policy()
    if not policy["rules"]:
        print("policy.json: no rules parsed; refusing to write an empty file")
        return 1
    target = DASHBOARD / "policy.json"
    target.write_text(json.dumps(policy, indent=1, ensure_ascii=False) + "\n",
                      encoding="utf-8", newline="\n")
    print(f"wrote {target.relative_to(ROOT).as_posix()}: "
          f"{len(policy['rules'])} rules, policy v{policy['version']}, "
          f"sha256 {policy['sha256'][:12]}")

    for name in COPIES:
        src = RESULTS / name
        if not src.exists():
            print(f"missing {src.relative_to(ROOT).as_posix()}; run the eval first")
            return 1
        shutil.copyfile(src, DASHBOARD / name)
        print(f"copied eval/results/{name} -> dashboard/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
