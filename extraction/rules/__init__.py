"""Rule-based parsers, one per known sender/layout.

Each module exposes:
  detect(content) -> bool        cheap check: does this look like our layout?
  parse(content) -> list[dict]   raw records (pre-normalization). Should not
                                  raise for minor per-row issues; skip the row.

The pipeline tries each rule's detect() in order and uses the first match's
parse(). If none match, it falls back to the LLM.
"""
from . import knotel, metspace, workplace_plus, gpe, bc, breezblok, union, spreadsheet_blocks, grid
from ..rule_sanity import records_look_plausible

RULES = [
    ("Knotel", knotel),
    ("MetSpace", metspace),
    ("Workplace Plus", workplace_plus),
    ("GPE", gpe),
    ("BC", bc),
    ("Breezblok", breezblok),
    ("UNION", union),
    ("Spreadsheet Blocks", spreadsheet_blocks),
    ("Grid/Tabular", grid),
]


def try_rules(content):
    """Returns (rule_name, records) for the first matching rule, or (None, None)."""
    for name, module in RULES:
        try:
            if module.detect(content):
                records = module.parse(content)
                if records:
                    if records_look_plausible(records):
                        return name, records
                    # Confirmed real (2026-07, MetSpace's own "Office Of
                    # The Week" single-listing template): detect() can
                    # correctly recognize the sender while parse() still
                    # produces garbage, because the email's actual
                    # structure doesn't match the ONE layout this rule
                    # was built against — see extraction.rule_sanity's
                    # own docstring for the real failure mode. Treated
                    # exactly like the rule not matching at all, so this
                    # falls through to the LLM fallback instead of
                    # silently accepting a result nothing else in the
                    # pipeline had any reason to distrust.
                    print(
                        f"[rules] '{name}' matched but its own output looked implausible "
                        "(likely an email/document template variant this rule wasn't built for) "
                        "— falling back to the LLM instead of trusting it"
                    )
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            # A rule erroring out just means it doesn't apply cleanly —
            # fall through to the next rule / eventually the LLM.
            print(f"[rules] '{name}' parser rejected this input: {type(exc).__name__}: {exc}")
            continue
    return None, None
