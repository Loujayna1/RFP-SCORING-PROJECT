"""
Rule-based evaluation_mode classifier for EvaluationCriterion records.

Each leaf criterion is assigned an evaluation_mode that drives which scorer
is used during vendor evaluation.  Classification is deterministic (regex
patterns), not LLM-driven, so it works across any evaluation matrix whose
vocabulary changes from client to client.

Supported modes
---------------
requirement_coverage   — functional/technical/security/operational/infra requirements
plan_methodology       — project plans, roadmaps, timelines, methodologies, deliverables
experience_references  — references, portfolio, case studies, prior implementations
team_capability        — team, resources, CVs, certifications, staffing
solution_quality       — proposal quality, clarity, coherence, architecture quality
commercial             — pricing, cost, financial proposal, commercial terms

To extend: add new regex strings to the relevant MODE_RULES list.
Patterns are matched against "<label> <parent_label>" (case-insensitive).
First matching mode wins, in the order defined in MODE_RULES.
"""

import re
from typing import Optional

# Each key maps to an ordered list of regex patterns.
# Patterns are checked against the combined "label parent_label" string.
# First match wins — more-specific patterns should come first within each mode.
MODE_RULES: dict[str, list[str]] = {
    "requirement_coverage": [
        r"\bfunctional\s+req",
        r"\btechnical\s+req",
        r"\bsecurity\s+req",
        r"\boperational\s+req",
        r"\binfrastructure\s+req",
        r"\bintegration\s+req",
        r"\bperformance\s+req",
        r"\bcompliance\s+req",
        r"\bregulatory\s+req",
        r"\bsystem\s+req",
        r"\brequirements?\b",
        r"\bspecification\b",
        r"\bconformance\b",
    ],
    "plan_methodology": [
        r"\bproject\s+plan\b",
        r"\bimplementation\s+plan\b",
        r"\bdeployment\s+plan\b",
        r"\bmethodology\b",
        r"\bimplementation\s+approach\b",
        r"\bproject\s+approach\b",
        r"\bwork\s+plan\b",
        r"\bwork\s+breakdown\b",
        r"\broadmap\b",
        r"\btimeline\b",
        r"\bmilestone",
        r"\bdeliverable",
        r"\bgovernance\s+(model|plan|framework)\b",
        r"\brollout\b",
        r"\btransition\s+plan\b",
        r"\bchange\s+management\b",
        r"\bproject\s+governance\b",
        r"\bexecution\s+plan\b",
    ],
    "experience_references": [
        r"\breference",
        r"\bportfolio\b",
        r"\bsimilar\s+project",
        r"\bcase\s+stud",
        r"\bprior\s+(experience|implementation|project|deployment)",
        r"\bsector\s+(experience|expertise)",
        r"\bdomain\s+(experience|expertise)",
        r"\bcustomer\s+(base|reference|list|project)",
        r"\bclient\s+(base|reference|list|project|experience)",
        r"\bcomparable\s+implementation",
        r"\btrack\s+record\b",
        r"\bpast\s+(project|implementation|experience|deployment)",
        r"\bindustry\s+experience\b",
        r"\btelecom\s+experience\b",
        r"\btelecommunication\s+experience\b",
        r"\bproject\s+experience\b",
    ],
    "team_capability": [
        r"\bproposed\s+(team|staff|resource|personnel)\b",
        r"\bkey\s+(personnel|staff|resource|consultant)",
        r"\bteam\s+(size|composition|structure|strength|profile|expertise)\b",
        r"\bnumber\s+of\s+(resource|staff|consultant|person|team)",
        r"\bexperience\s+of\s+(the\s+)?(resource|consultant|staff|team|personnel|proposed)",
        r"\byears\s+of\s+(experience|expertise)\b",
        r"\bproposed\s+resource",
        r"\bstaffing\b",
        r"\bcvs?\b",                       # matches both "CV" and "CVs"
        r"\bres[uü]m[eé]?\b",
        r"\btechnical\s+certification",
        r"\bcertification",                 # no trailing \b → matches plural
        r"\bqualification",                 # no trailing \b → matches plural
        r"\bseniority\b",
        r"\bnumber\s+of\s+assigned",
        r"\bexperience\s+of\s+assigned",
    ],
    "solution_quality": [
        r"\bquality\s+of\s+(the\s+)?(submission|response|proposal|solution|deliverable|offer|document)\b",
        r"\bclarity\s+(of|and)\s+(the\s+)?(response|proposal|submission|document|offer)\b",
        r"\bcompleteness\s+(of|and)\s+(the\s+)?(response|proposal|submission)\b",
        r"\bcoherence\b",
        r"\brelevance\s+of\s+(the\s+)?(proposal|response|solution)\b",
        r"\binnovation\b",
        r"\barchitecture\s+quality\b",
        r"\bsolution\s+design\b",
        r"\boverall\s+(quality|proposal|solution)\b",
        r"\bpresentation\s+quality\b",
    ],
    "commercial": [
        r"\bpric",
        r"\bcost\b",
        r"\bfinancial\s+proposal\b",
        r"\bcommercial\s+(offer|proposal|terms|model)\b",
        r"\btco\b",
        r"\btotal\s+cost\s+of\s+ownership\b",
        r"\blicens",
        r"\bfee\s+structure\b",
        r"\bpayment\s+terms?\b",
        r"\bbudget\b",
    ],
}

_DEFAULT_MODE = "solution_quality"


def classify_evaluation_mode(
    label: str,
    parent_label: Optional[str] = None,
) -> str:
    """
    Return the evaluation_mode for a leaf criterion.

    Matches against "<label> <parent_label>" (case-insensitive).
    Returns the mode of the first matching pattern; falls back to
    _DEFAULT_MODE when nothing matches.
    """
    def _normalise(text: str) -> str:
        # Common spellings found in real matrices (French-influenced English).
        t = text.lower()
        t = re.sub(r"\bress?ources?\b", lambda m: "resources" if m.group(0).endswith("s") else "resource", t)
        t = re.sub(r"\baffected\b", "assigned", t)
        return t

    def _match(text: str) -> Optional[str]:
        for mode, patterns in MODE_RULES.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    return mode
        return None

    # The criterion's own label is more specific than its parent's: a
    # "Team certifications" criterion grouped under "References" is a team
    # criterion. The parent label is only used when the label alone is silent.
    return (
        _match(_normalise(label))
        or _match(_normalise(f"{label} {parent_label or ''}"))
        or _DEFAULT_MODE
    )

