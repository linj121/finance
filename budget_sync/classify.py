"""Category assignment. Precedence, highest first:
  manual  - you changed the Category cell in Google Sheets (never overwritten)
  rule    - a keyword rule in rules.yaml
  learned - same description as a transaction you edited manually
  ai      - Claude's answer, remembered per description so each is asked only once
"""
import json
import re
from pathlib import Path

import yaml

from .models import Transaction

UNCATEGORIZED = "Uncategorized"
# Bank transaction-type prefixes: a purchase and its reversal/correction should share a
# category so they net out.
TXN_TYPE_PREFIX = re.compile(
    r"^(VISA DEBIT (PURCHASE|REVERSAL|CORRECTION|AUTHORIZATION EXPIRED|AUTH REVERSAL EXPIRED)"
    r"|(CONTACTLESS )?INTERAC PURCHASE)\s*-\s*")


def normalize(desc: str) -> str:
    """Strip store numbers, card digits and punctuation so 'STARBUCKS #1234' == 'STARBUCKS #987'."""
    # e-Transfer reference codes (E2YUTT, C1AJZs72qdww, 32-char hex) mix letters and digits
    s = " ".join(w for w in desc.split()
                 if not (len(w) >= 5 and re.search(r"\d", w) and re.search(r"[A-Za-z]", w) and w.isalnum()))
    s = s.upper()
    s = re.sub(r"#\s*\d+|\d{3,}|[*/\\]", " ", s)
    s = " ".join(s.split())
    return TXN_TYPE_PREFIX.sub("", s)


class Classifier:
    def __init__(self, rules_path: Path, memory_path: Path, ai_cfg: dict):
        cfg = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        self.categories: dict[str, str] = cfg["categories"]
        self.rules = [(_compile(r["match"]), r["category"]) for r in cfg.get("rules") or []]
        for _, cat in self.rules:
            if cat not in self.categories:
                raise ValueError(f"rules.yaml: rule uses unknown category {cat!r}")
        self.memory_path = memory_path
        self.memory: dict[str, dict] = (json.loads(memory_path.read_text(encoding="utf-8"))
                                        if memory_path.exists() else {})
        self.ai_cfg = ai_cfg

    def remember(self, desc: str, category: str, source: str) -> None:
        self.memory[normalize(desc)] = {"category": category, "source": source}

    def save(self) -> None:
        self.memory_path.write_text(json.dumps(self.memory, indent=1, ensure_ascii=False, sort_keys=True),
                                    encoding="utf-8")

    def classify(self, txns: list[Transaction], use_ai: bool) -> None:
        todo = [t for t in txns if t.category_source != "manual"]
        unknown: dict[str, list[Transaction]] = {}
        for t in todo:
            cat = next((c for rx, c in self.rules if rx.search(t.description)), None)
            if cat:
                t.category, t.category_source = cat, "rule"
                continue
            mem = self.memory.get(normalize(t.description))
            if mem and mem["category"] in self.categories:
                # keep where the answer originally came from
                t.category = mem["category"]
                t.category_source = "ai" if mem["source"] == "ai" else "learned"
                continue
            unknown.setdefault(normalize(t.description), []).append(t)

        if unknown and use_ai:
            answers = self._ask_claude(unknown)
            for norm, cat in answers.items():
                self.memory[norm] = {"category": cat, "source": "ai"}
                for t in unknown.pop(norm):
                    t.category, t.category_source = cat, "ai"
        for group in unknown.values():
            for t in group:
                t.category, t.category_source = UNCATEGORIZED, ""

    def _ask_claude(self, unknown: dict[str, list[Transaction]]) -> dict[str, str]:
        import anthropic

        client = anthropic.Anthropic()
        cats = [c for c in self.categories if c != UNCATEGORIZED]
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "category": {"type": "string", "enum": cats}},
                "required": ["index", "category"], "additionalProperties": False}}},
            "required": ["items"], "additionalProperties": False,
        }
        system = (
            "You categorize personal bank and credit card transactions for a household budget in Canada. "
            "Pick exactly one category per transaction from this list:\n"
            + "\n".join(f"- {c}: {self.categories[c]}" for c in cats)
            + "\nNegative amounts are money out, positive are money in. "
            "Use 'Other' only when the description gives no usable clue."
        )
        keys = list(unknown)
        result: dict[str, str] = {}
        size = int(self.ai_cfg.get("batch_size", 60))
        for i in range(0, len(keys), size):
            chunk = keys[i:i + size]
            lines = []
            for j, k in enumerate(chunk):
                t = unknown[k][0]
                lines.append(f"{j}. [{t.account}] {t.description} | amount {t.amount}")
            resp = client.beta.messages.create(
                model=self.ai_cfg.get("model", "claude-opus-5-5"),
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": self.ai_cfg.get("effort", "low"),
                               "format": {"type": "json_schema", "schema": schema}},
                system=system,
                messages=[{"role": "user", "content": "Categorize:\n" + "\n".join(lines)}],
            )
            if resp.stop_reason in ("refusal", "max_tokens"):
                print(f"  ! Claude stopped ({resp.stop_reason}) on batch {i // size + 1}; left uncategorized")
                continue
            text = next(b.text for b in resp.content if b.type == "text")
            for item in json.loads(text)["items"]:
                if 0 <= item["index"] < len(chunk):
                    result[chunk[item["index"]]] = item["category"]
            print(f"  Claude categorized {len(chunk)} new merchants")
        return result


def _compile(match: str) -> re.Pattern:
    if match.startswith("re:"):
        return re.compile(match[3:], re.I)
    return re.compile(re.escape(match), re.I)
