"""
All LLM interactions go through this module.
Each function calls the Groq rotation client and parses structured JSON output.

"""
import json
import logging
import re
from typing import Optional, Callable, Awaitable, AsyncIterator

from app.core.groq_rotation import get_groq_client
from app.core.config import settings

logger = logging.getLogger(__name__)

AGENT_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_database",
        "description": (
            "Search the Milvus knowledge base for evidence about an RFP topic. "
            "Use vendor_name when comparing a specific vendor."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The topic, capability, requirement, or comparison point to search for.",
                },
                "vendor_name": {
                    "type": ["string", "null"],
                    "description": "Optional vendor name to narrow the search to one vendor.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

AGENT_PRICING_TOOL = {
    "type": "function",
    "function": {
        "name": "get_vendor_pricing",
        "description": "Retrieve pricing information, quotes, or financial excerpts for a specific vendor from the database.",
        "parameters": {
            "type": "object",
            "properties": {
                "vendor_name": {
                    "type": "string",
                    "description": "The name of the vendor (e.g. 'Vendor A' or 'CloudCorp') to retrieve pricing for.",
                },
            },
            "required": ["vendor_name"],
            "additionalProperties": False,
        },
    },
}

AGENT_COMPARE_TOOL = {
    "type": "function",
    "function": {
        "name": "compare_vendors",
        "description": "Compare two vendors on their scores, compliance levels, or response evidence for a specific requirement or requirement code.",
        "parameters": {
            "type": "object",
            "properties": {
                "requirement_id": {
                    "type": "string",
                    "description": "The requirement code (e.g., 'BG.FUN.001') or a query description of the requirement to compare.",
                },
                "vendor_a": {
                    "type": "string",
                    "description": "First vendor name (e.g., 'Vendor A').",
                },
                "vendor_b": {
                    "type": "string",
                    "description": "Second vendor name (e.g., 'Vendor B').",
                },
            },
            "required": ["requirement_id", "vendor_a", "vendor_b"],
            "additionalProperties": False,
        },
    },
}

AGENT_MATRIX_TOOL = {
    "type": "function",
    "function": {
        "name": "get_rfp_matrix_weights",
        "description": "Retrieve the evaluation criteria categories, their weights, and maximum scores for the current RFP.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

AGENT_RANKINGS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_vendor_rankings",
        "description": (
            "Retrieve the official computed scores and rankings for ALL vendors in this RFP. "
            "Returns total scores, per-criterion section scores, compliance level breakdown, "
            "and coverage statistics. "
            "ALWAYS call this tool first when the user asks for a summary, ranking, comparison, "
            "recommendation, or overall evaluation of vendors."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

AGENT_RFP_OVERVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "get_rfp_overview",
        "description": (
            "Retrieve a structured overview of the RFP itself: total extracted requirement count, "
            "mandatory vs optional counts, per-category breakdown, and evaluation criteria weights. "
            "ALWAYS call this tool first when the user asks for an RFP overview, requirement count, "
            "summary of what the RFP contains, key obligations, or structure before any vendors are evaluated."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

# System prompt used when no vendor responses have been uploaded yet (Stage A).
_RFP_OVERVIEW_SYSTEM_PROMPT = (
    "You are a senior procurement analysis agent for an RFP evaluation platform.\n\n"
    "CURRENT STAGE: RFP uploaded — no vendor responses have been submitted yet.\n\n"
    "═══ YOUR TASK ═══\n"
    "Summarize the RFP document itself. Help the procurement team understand what this RFP "
    "demands before any vendor evaluation begins.\n\n"
    "═══ TOOL PRIORITY — follow strictly ═══\n"
    "1. Requirement counts, categories, RFP structure, obligations → call get_rfp_overview FIRST. "
    "Use the exact total_requirements number it returns — do not invent or guess a count.\n"
    "2. Specific text, clauses, or obligations → search_database.\n"
    "3. Evaluation criteria weights → get_rfp_matrix_weights.\n\n"
    "═══ WHAT TO INCLUDE IN YOUR RESPONSE ═══\n"
    "- Total extracted requirement count (exact number from get_rfp_overview)\n"
    "- Mandatory vs optional requirement breakdown\n"
    "- Categories / sections of the RFP and their requirement counts\n"
    "- Key compliance obligations\n"
    "- Scoring / weighting structure if criteria are available\n"
    "- Deadlines / submission constraints if found\n"
    "- Ambiguities, risks, or heavy evaluation areas\n\n"
    "═══ STRICT PROHIBITIONS ═══\n"
    "No vendor responses are available yet. You MUST NOT:\n"
    "- Produce vendor rankings, profiles, or comparisons\n"
    "- Mention decision confidence or vendor recommendations\n"
    "- Produce any compliance summary of vendors\n"
    "- Invent vendor data or speculate about vendor responses\n\n"
    "═══ OUTPUT FORMAT ═══\n"
    "## RFP Overview\n"
    "State the RFP title (if known) and the exact total requirement count.\n\n"
    "## Requirement Breakdown\n"
    "Mandatory count, optional count, and per-category counts.\n\n"
    "## Evaluation Criteria & Weighting\n"
    "If criteria weights are available, list them.\n\n"
    "## Key Obligations & Risks\n"
    "Highlight heavy-weight areas, strict compliance requirements, and any ambiguities.\n\n"
    "## Submission Constraints\n"
    "Deadlines, document requirements, and format rules if found."
)



_EXTRACT_CONTENT_LIMIT = 8000


def _parse_json(text: str) -> dict | list:
    """
    Strip markdown fences and parse JSON safely.

    """
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    decoder = json.JSONDecoder(strict=False)
    try:
        value, end = decoder.raw_decode(cleaned)
        trailing = cleaned[end:].strip()
        if trailing:
            logger.warning(
                "_parse_json: trailing content after first JSON value (%d chars): %r",
                len(trailing), trailing[:80],
            )
        return value
    except json.JSONDecodeError:
        for idx, char in enumerate(cleaned):
            if char not in "[{":
                continue
            try:
                value, end = decoder.raw_decode(cleaned[idx:])
            except json.JSONDecodeError:
                continue
            trailing = cleaned[idx + end:].strip()
            if trailing:
                logger.warning(
                    "_parse_json fallback: trailing content (%d chars): %r",
                    len(trailing), trailing[:80],
                )
            return value
        raise


def _parse_json_array(text: str) -> list:
    """
    Parse a JSON array of objects, tolerating truncation.

    LLM array responses are regularly cut off at max_tokens mid-object. A plain
    json.loads then fails, and _parse_json's fallback recovers only the FIRST
    object as a dict — which callers that expect a list silently drop, losing
    every item. This salvages all COMPLETE top-level objects and discards only
    the final partial one.
    """
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()

    # Fast path: the WHOLE payload is valid JSON (a complete array or a lone
    # object). Must be strict on the whole string — a lenient per-value decode
    # would "succeed" on a truncated array by returning only its first object.
    try:
        value = json.loads(cleaned, strict=False)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
    except json.JSONDecodeError:
        pass

    # Salvage path: walk top-level objects inside a truncated `[ ... ]`.
    decoder = json.JSONDecoder(strict=False)
    items: list = []
    i = cleaned.find("[")
    i = 0 if i == -1 else i + 1
    n = len(cleaned)
    while i < n:
        while i < n and cleaned[i] in " \t\r\n,":
            i += 1
        if i >= n or cleaned[i] == "]":
            break
        if cleaned[i] != "{":
            break
        try:
            obj, end = decoder.raw_decode(cleaned, i)
        except json.JSONDecodeError:
            break  # final object was truncated — keep the complete ones
        if isinstance(obj, dict):
            items.append(obj)
        i = end
    if items:
        logger.warning(
            "_parse_json_array: salvaged %d complete object(s) from a malformed/"
            "truncated array response (%d chars)",
            len(items), len(cleaned),
        )
    return items


# ── 1. DocRole classifier ──────────────────────────────────────────────────────
async def classify_doc_role(file_name: str, content_preview: str) -> str:
    """
    Returns one of: RFPSpec | EvalMatrix | VendorProposal | ComplianceMatrix |
                    CV | Certificate | Pricing | Legal | Supporting
    """
    groq = get_groq_client()
    prompt = (
        f"You are classifying a document in an RFP evaluation system.\n"
        f"File name: {file_name}\n"
        f"Content preview (first 500 chars): {content_preview[:500]}\n\n"
        f'Respond with ONLY a JSON object: {{"role": "<role>"}}\n'
        f"Roles: RFPSpec, EvalMatrix, VendorProposal, ComplianceMatrix, CV, Certificate, Pricing, Legal, Supporting"
    )
    result = await groq.chat_text(
        [{"role": "user", "content": prompt}],
        model=settings.GROQ_FAST_MODEL,
        max_tokens=50,
    )
    try:
        return _parse_json(result)["role"]
    except Exception:
        return "Supporting"


# ── 2. Requirement extractor ───────────────────────────────────────────────────
async def extract_requirements(doc_content: str, doc_role: str, page_number: int = 1) -> list[dict]:
    """
    Extracts structured requirements from RFP documents.
    Returns list of: {req_code, description, category, is_mandatory, source_page}


    """
    groq = get_groq_client()

    # ISSUE #20 FIX: log truncation so it's visible in logs
    content = doc_content
    if len(doc_content) > _EXTRACT_CONTENT_LIMIT:
        logger.info(
            "extract_requirements: truncating content from %d to %d chars",
            len(doc_content), _EXTRACT_CONTENT_LIMIT,
        )
        content = doc_content[:_EXTRACT_CONTENT_LIMIT]

    prompt = (
        f"You are an RFP analyst. Extract all requirements from this document section.\n"
        f"Document role: {doc_role}\n"
        f"The content is split by [PAGE n] markers showing the page each passage is on.\n"
        f"Content:\n{content}\n\n"
        f"Return ONLY a JSON array. Each item: {{\n"
        f'  "req_code": "e.g. BG.FUN.001 or auto-generate if missing",\n'
        f'  "description": "clear requirement description",\n'
        f'  "category": "the RFP section/topic this requirement belongs to. Prefer the '
        f"document's own section name or numbered heading (e.g. 'Business', 'Technical', "
        f"'Support', 'Certifications & Compliance', 'Project', 'Pricing'). Use a short, "
        f'consistent label — not a generic guess",\n'
        f'  "is_mandatory": true or false,\n'
        f'  "source_page": "the number from the [PAGE n] marker of the passage this '
        f'requirement came from",\n'
        f'  "original_text": "verbatim sentence or paragraph from the content where this was found"\n'
        f"}}\nReturn [] if no requirements found."
    )
    result = await groq.chat_text(
        [{"role": "user", "content": prompt}],
        model=settings.GROQ_MAIN_MODEL,
        max_tokens=8000,
    )
    try:
        data = _parse_json_array(result)
        if not data:
            logger.warning(
                "extract_requirements: no requirements parsed from %d-char response "
                "(role=%s, page=%s)",
                len(result), doc_role, page_number,
            )
        return data
    except Exception as exc:
        logger.warning("Requirement extraction parse error: %s", exc)
        return []


async def filter_novel_requirements(textual: list[dict], matrix: list[dict]) -> list[dict]:
    """Return the subset of `textual` requirements NOT already covered by `matrix`.

    The Excel matrix is authoritative; PDF/Word prose should contribute only
    genuinely new obligations. Coverage is judged semantically by the LLM, which
    (unlike a cosine threshold) can tell that "provide a ticketing platform" and
    "ticket & incident management" are the same requirement. On any parse failure
    or an empty/garbage result the full textual set is kept — this filter never
    silently wipes requirements.
    """
    if not textual or not matrix:
        return textual

    def _desc(r: dict) -> str:
        return (r.get("description") or r.get("original_text") or "").strip()

    matrix_block = "\n".join(f"M{i}. {_desc(r)[:200]}" for i, r in enumerate(matrix))
    cand_block = "\n".join(f"T{i}. {_desc(r)[:200]}" for i, r in enumerate(textual))
    prompt = (
        "You are deduplicating RFP requirements.\n\n"
        "OFFICIAL requirements already captured from the compliance matrix:\n"
        f"{matrix_block}\n\n"
        "CANDIDATE requirements extracted from the RFP prose document:\n"
        f"{cand_block}\n\n"
        "Return ONLY a JSON array of the integer indices i of the candidates Ti "
        "whose obligation is NOT already covered by ANY official requirement above "
        "— the genuinely NEW requirements to keep. A candidate is 'covered' if an "
        "official requirement expresses the same obligation, even if worded "
        "differently or more broadly. Example: [0, 4, 7]. Return [] only if every "
        "candidate is already covered."
    )

    groq = get_groq_client()
    try:
        result = await groq.chat_text(
            [{"role": "user", "content": prompt}],
            model=settings.GROQ_MAIN_MODEL,
            max_tokens=2000,
        )
        raw = _parse_json(result)
    except Exception as exc:
        logger.warning("filter_novel_requirements failed (%s); keeping all textual", exc)
        return textual

    keep_idx: set[int] = set()
    for x in raw if isinstance(raw, list) else []:
        m = re.search(r"\d+", str(x))
        if m:
            idx = int(m.group())
            if 0 <= idx < len(textual):
                keep_idx.add(idx)

    kept = [r for i, r in enumerate(textual) if i in keep_idx]
    if not kept:
        # Model returned nothing usable — keep all rather than drop everything.
        logger.warning(
            "filter_novel_requirements: no valid indices returned; keeping all %d textual",
            len(textual),
        )
        return textual

    logger.info(
        "filter_novel_requirements: kept %d of %d textual requirements "
        "(dropped %d already covered by the matrix)",
        len(kept), len(textual), len(textual) - len(kept),
    )
    return kept


# ── 3. Vendor claim extractor ──────────────────────────────────────────────────
async def extract_vendor_claims(
    chunk_content: str,
    doc_role: str,
    source_page: int,
    source_section: str,
) -> list[dict]:
    """
    Extracts discrete vendor claims (capabilities, certifications, tech specs) from a chunk.
    Returns list of: {claim_text, claim_type, source_page, source_section}
    """
    groq = get_groq_client()
    prompt = (
        f"You are analyzing a vendor's RFP response document.\n"
        f"Document role: {doc_role} | Section: {source_section} | Page: {source_page}\n\n"
        f"Content:\n{chunk_content[:2500]}\n\n"
        f"Extract all discrete capability claims or compliance statements the vendor makes.\n"
        f"Return ONLY a JSON array. Each item: {{\n"
        f'  "claim_text": "the exact claim in vendor\'s words",\n'
        f'  "claim_type": "Capability|Certification|TechSpec|Compliance|Reference|Pricing|TeamSkill",\n'
        f'  "source_page": {source_page},\n'
        f'  "source_section": "{source_section}"\n'
        f"}}\nReturn [] if no claims found."
    )
    result = await groq.chat_text(
        [{"role": "user", "content": prompt}],
        model=settings.GROQ_FAST_MODEL,
        max_tokens=1500,
    )
    try:
        return _parse_json_array(result)
    except Exception as exc:
        logger.warning("Claim extraction parse error: %s", exc)
        return []


# ── 4. CV / Resume structured extractor ────────────────────────────────────────
async def extract_cv_data(ocr_text: str, source_page: int) -> dict:
    """
    Extracts structured profile from OCR text of a CV or resume.
    Returns: {full_name, role, years_experience, skills, certifications, education}
    """
    groq = get_groq_client()
    prompt = (
        f"Extract structured profile data from this CV/resume OCR text.\n"
        f"Page: {source_page}\n\n"
        f"Text:\n{ocr_text[:3000]}\n\n"
        f"Return ONLY a JSON object: {{\n"
        f'  "full_name": "...",\n'
        f'  "role": "job title / role",\n'
        f'  "years_experience": <integer>,\n'
        f'  "skills": ["skill1", "skill2"],\n'
        f'  "certifications": ["cert1"],\n'
        f'  "education": [{{"degree": "...", "institution": "...", "year": 2018}}]\n'
        f"}}"
    )
    result = await groq.chat_text(
        [{"role": "user", "content": prompt}],
        model=settings.GROQ_FAST_MODEL,
        max_tokens=800,
    )
    try:
        return _parse_json(result)
    except Exception:
        return {
            "full_name": "Unknown", "role": "Unknown",
            "years_experience": 0, "skills": [],
            "certifications": [], "education": [],
        }


# ── 5. Chain-of-Thought Semantic Scoring ──────────────────────────────────────

_COT_SYSTEM_PROMPT = """\
You are a senior RFP technical evaluator. A vendor has declared, in its \
compliance matrix, how far it meets a requirement. Your job is to VERIFY that \
declaration against the vendor's proposal: decide which compliance level the \
passages of the proposal actually support. You are not told what the vendor \
declared; judge only from the passages.

How to decide
- Break the requirement into its conditions (each capability, scope item, \
number, standard or deadline it states).
- For each condition, find what the passages say. Numbers must be compared: \
"RPO of 5 minutes" meets "RPO of 15 minutes or less"; "99.5%" does not meet \
"at least 99.95%"; "quarterly" does not meet "at least monthly".
- A concrete statement of a capability available today counts as evidence. Do \
not lower the level because a statement is not independently certified, unless \
the requirement itself asks for a certification.
- Ignore generic sentences that could apply to any requirement ("we comply with \
all the Client's requirements", "see the compliance matrix").
- If no passage concerns this requirement, the requirement is not addressed, \
even if the passages describe related features.

YOU MUST follow this exact two-phase structure in your response:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 1 — REASONING (think before you decide)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[COMPLETENESS] List the conditions of the requirement and, for each one, \
whether the passages meet it, meet it only in part, or do not mention it.

[SPECIFICITY] Quote the figures, standards or named features the passages \
give, and compare each to the requirement.

[RELEVANCE] Say which chunk(s) concern this requirement, and which do not.

[RISK / GAPS] Note explicit limitations, exclusions, future releases or \
statements that something is not supported.

[VERIFIABILITY] Note whether a certification, standard or test result that \
the requirement asks for is evidenced.

"""

# Definitions of the standard compliance levels. The levels actually offered to
# the model, and the points they are worth, come from the scale configured for
# the RFP by its administrator (compliance_levels); any level the administrator
# adds is offered with its own label.
_LEVEL_DEFINITIONS = {
    "fullycompliant":     ("FullyCompliant",
                           "the passages show the capability available today and meeting EVERY condition of the requirement"),
    "partiallycompliant": ("PartiallyCompliant",
                           "available today, but at least one condition is not met (lower figure, narrower scope, a missing part)"),
    "roadmapcompliant":   ("RoadmapCompliant",
                           "not available today; the vendor states it is planned for a future release"),
    "noncompliant":       ("NonCompliant",
                           "the passages state that the capability is not supported or not provided (alternatives proposed do not change this)"),
    "notcompliant":       ("NonCompliant",
                           "the passages state that the capability is not supported or not provided (alternatives proposed do not change this)"),
}
_NOT_ADDRESSED = {
    "name": "NotAddressed", "label": "Not Addressed", "points": 0.0,
    "definition": "no passage concerns this requirement: the claim is unsupported by the proposal",
}
DEFAULT_COMPLIANCE_SCALE = [
    {"label": "Fully Compliant",     "value": 5},
    {"label": "Partially Compliant", "value": 3},
    {"label": "Roadmap Compliant",   "value": 2},
    {"label": "Non-Compliant",       "value": 1},
]
SCORING_FAILED = "ScoringFailed"


def _level_key(text) -> str:
    return re.sub(r"[^a-z]", "", str(text or "").lower())


def build_compliance_scale(compliance_levels: Optional[list[dict]] = None) -> list[dict]:
    """Turn the RFP's configured levels into the scale used for scoring."""
    scale: list[dict] = []
    for lvl in (compliance_levels or DEFAULT_COMPLIANCE_SCALE):
        label = str(lvl.get("label", "")).strip()
        if not label:
            continue
        try:
            points = float(lvl.get("value", 0))
        except (TypeError, ValueError):
            points = 0.0
        name, definition = _LEVEL_DEFINITIONS.get(
            _level_key(label),
            (re.sub(r"[^A-Za-z]", "", label.title()), f"as defined by the evaluation committee for '{label}'"),
        )
        scale.append({"name": name, "label": label, "points": points, "definition": definition})
    if not any(_level_key(x["name"]) == "notaddressed" for x in scale):
        scale.append(dict(_NOT_ADDRESSED))
    return scale


def _verdict_instructions(scale: list[dict]) -> str:
    names = "|".join(x["name"] for x in scale)
    levels = "\n".join(f"  {x['name']:<20} — {x['definition']}" for x in scale)
    return (
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "PHASE 2 — VERDICT (JSON only, no extra text)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "After your reasoning, output EXACTLY this JSON and nothing else:\n\n"
        "{\n"
        '  "reasoning": "<copy your full Phase 1 analysis here as a single string>",\n'
        f'  "compliance_level": "<{names}>",\n'
        '  "evidence_chunk": <number of the chunk that decides the level, or 0 if none>,\n'
        '  "evidence_quote": "<the decisive sentence copied WORD FOR WORD from that chunk, or empty>",\n'
        '  "justification": "<1-2 sentences summarising WHY this level, citing evidence>"\n'
        "}\n\n"
        "Choose exactly one compliance level. You choose the LEVEL only; the score "
        "is derived from it by the evaluation committee's scale.\n"
        f"{levels}\n"
    )


def _normalise_score_result(parsed: dict, req_code: str, scale: Optional[list[dict]] = None) -> dict:
    """Map the model's level onto the configured scale. Raises on an unknown level."""
    scale = scale or build_compliance_scale()
    level = _level_key(parsed.get("compliance_level"))
    for entry in scale:
        if level in (_level_key(entry["name"]), _level_key(entry["label"])):
            return {
                "reasoning": str(parsed.get("reasoning", "")),
                "raw_score": entry["points"],
                "compliance_level": entry["name"],
                "evidence_chunk": parsed.get("evidence_chunk"),
                "evidence_quote": str(parsed.get("evidence_quote") or "").strip(),
                "justification": str(
                    parsed.get("justification")
                    or f"Requirement {req_code} assessed as {entry['name']}."
                ),
            }
    raise ValueError(f"unknown compliance level {parsed.get('compliance_level')!r}")


def _scoring_failed(req_code: str, reason: str) -> dict:
    """
    Result recorded when the model's answer cannot be used after a retry.
    It is not a judgment: it carries no score into the aggregation and is
    always flagged for human review.
    """
    return {
        "reasoning": "",
        "raw_score": 0.0,
        "compliance_level": SCORING_FAILED,
        "justification": (
            f"Automated assessment of requirement {req_code} failed ({reason}). "
            "Human review required."
        ),
        "scoring_failed": True,
    }


async def score_requirement(
    requirement_description: str,
    req_code: str,
    evidence_text: Optional[str],
    similarity_score: float,
    compliance_levels: Optional[list[dict]] = None,
) -> dict:
    """
    Chain-of-Thought compliance assessment of one requirement.

    The model reasons through five dimensions (completeness, specificity,
    relevance, risks and gaps, verifiability) and then chooses a compliance
    level from the scale configured for the RFP. The score is derived from the
    level through that scale, never chosen by the model. Temperature 0 keeps
    the assessment as reproducible as the provider allows.

    If the answer cannot be parsed, or names a level outside the scale, the
    model is asked once more; if it fails again the requirement is recorded as
    ScoringFailed (no score, flagged for review) rather than given a default.
    """
    scale = build_compliance_scale(compliance_levels)

    if not evidence_text:
        not_addressed = next(x for x in scale if x["name"] == "NotAddressed")
        return {
            "reasoning": (
                f"[COMPLETENESS] No vendor evidence retrieved for {req_code}.\n"
                f"[SPECIFICITY] N/A\n"
                f"[RELEVANCE] N/A\n"
                f"[RISK / GAPS] Requirement is completely unaddressed.\n"
                f"[VERIFIABILITY] N/A"
            ),
            "raw_score": not_addressed["points"],
            "compliance_level": "NotAddressed",
            "justification": f"No vendor evidence found for requirement {req_code}.",
            # Distinguishes a retrieval/coverage gap from a vendor that was
            # evaluated and judged non-compliant. Always flag for human review.
            "no_evidence": True,
        }

    groq = get_groq_client()
    system_prompt = _COT_SYSTEM_PROMPT + _verdict_instructions(scale)
    names = ", ".join(x["name"] for x in scale)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"REQUIREMENT  ({req_code}):\n"
            f"{requirement_description}\n\n"
            f"PASSAGES FROM THE VENDOR'S PROPOSAL (numbered chunks):\n"
            f"{evidence_text[:12000]}\n\n"
            f"Use exactly one compliance_level among: {names}.\n"
            f"Follow the two-phase structure in your system instructions exactly.\n"
            f"Think carefully through each reasoning dimension, then output the JSON verdict."
        )},
    ]

    last_error = "no answer"
    for attempt in range(2):
        try:
            result = await groq.chat_text(
                messages,
                model=settings.GROQ_MAIN_MODEL,
                max_tokens=2000,       # CoT reasoning + JSON verdict with quote
                temperature=0,
            )
            return _normalise_score_result(_parse_json(result), req_code, scale)
        except Exception as exc:
            last_error = str(exc)[:200]
            logger.warning("score_requirement attempt %d failed for %s: %s", attempt + 1, req_code, exc)
            messages = messages + [{
                "role": "user",
                "content": (
                    "Your previous answer could not be used. Answer again with the two "
                    f"phases and a valid JSON verdict whose compliance_level is one of: {names}."
                ),
            }]
    return _scoring_failed(req_code, last_error)


# ── 6. Agentic RAG answer generation ─────────────────────────────────────────
async def _call_search_database(
    search_database: Callable[[str, Optional[str]], Awaitable[list[dict]]],
    query: str,
    vendor_name: Optional[str] = None,
) -> dict:
    hits = await search_database(query, vendor_name)
    return {"query": query, "vendor_name": vendor_name, "hit_count": len(hits), "hits": hits}


async def agent_chat_loop(
    question: str,
    history: list[dict] | None,
    search_database: Callable[[str, Optional[str]], Awaitable[list[dict]]],
    max_tool_calls: int = 5,
    get_vendor_pricing: Optional[Callable[[str], Awaitable[dict]]] = None,
    compare_vendors: Optional[Callable[[str, str, str], Awaitable[dict]]] = None,
    get_rfp_matrix_weights: Optional[Callable[[], Awaitable[dict]]] = None,
    get_vendor_rankings: Optional[Callable[[], Awaitable[dict]]] = None,
    get_rfp_overview: Optional[Callable[[], Awaitable[dict]]] = None,
) -> AsyncIterator[dict]:
    """
    Multi-hop agent loop using Groq tool calling.

    Yields events:
    - {"type": "thought",   "content": "..."}   — tool-call thought
    - {"type": "citations", "data": [...]}       — source citations
    - {"type": "token",     "content": "..."}   — answer tokens
    - {"type": "done"}
    """
    groq = get_groq_client()

    # Stage A (RFP only, no vendors) uses a focused RFP-overview prompt.
    # Stage B (vendors present) uses the full evaluation-summary prompt.
    if get_rfp_overview is not None:
        _system_content = _RFP_OVERVIEW_SYSTEM_PROMPT
    else:
        _system_content = (
            "You are a senior procurement analysis agent for an RFP evaluation platform.\n\n"

            "═══ TOOL PRIORITY — follow strictly ═══\n"
            "1. Summary / ranking / recommendation / overall evaluation → call get_vendor_rankings FIRST. "
            "Never answer these from document search alone.\n"
            "2. Pricing / financial questions → get_vendor_pricing.\n"
            "3. Head-to-head on a specific requirement → compare_vendors.\n"
            "4. Criteria weights → get_rfp_matrix_weights.\n"
            "5. Specific evidence quotes → search_database (only AFTER structured tools).\n\n"

            "═══ SCORE INTERPRETATION RULES ═══\n"
            "Use the section_scores[criterion].interpretation field from get_vendor_rankings:\n"
            "  strength   = score ≥ 3.5  — vendor demonstrably covers this criterion well\n"
            "  neutral    = 2.5–3.4       — partial coverage, not a strength or a gap\n"
            "  gap        = 0 < score < 2.5 — vendor partially or weakly addresses this; state WHAT is missing\n"
            "  not_scored = 0             — no evidence found for this criterion\n"
            "NEVER label a criterion with score < 3.5 as a strength. "
            "NEVER label a criterion as a gap without stating what the gap is.\n\n"

            "═══ COMPLIANCE BREAKDOWN RULES ═══\n"
            "When reporting compliance_breakdown, always interpret the numbers:\n"
            "- If PartiallyCompliant > 60% of total: state 'the majority of requirements are partially addressed — "
            "vendor covers most topics but lacks specificity or completeness on many items.'\n"
            "- If NotAddressed > 15%: flag as a significant coverage risk.\n"
            "- If FullyCompliant < 10%: flag as low evidence quality — few requirements are explicitly and "
            "verifiably met.\n"
            "- If flagged_ratio > 0.30: warn that over 30% of scores have low confidence and human review "
            "is required before making a procurement decision.\n\n"

            "═══ RECOMMENDATION CONFIDENCE RULES ═══\n"
            "Use the decision_confidence field from get_vendor_rankings:\n"
            "- If confidence is LOW: explicitly state the scores are too close for a decisive recommendation "
            "and that human review is required. Do NOT pick a winner.\n"
            "- If confidence is MODERATE: name the leading vendor but note the narrow margin.\n"
            "- If confidence is HIGH: recommend clearly with justification from section scores.\n\n"

            "═══ OUTPUT CLEANLINESS RULES ═══\n"
            "- Never include tool errors, 'vendor not found' messages, or raw metadata in your answer.\n"
            "- Risks must be vendor-specific: cite the criterion name and score, not generic contract risks.\n"
            "- Gaps must describe WHAT is missing, not just show the number.\n"
            "- If a tool returns an error, say 'data unavailable for X' and continue.\n\n"

            "═══ SUMMARY FORMAT ═══\n"
            "## Ranking\n"
            "Rank each vendor: <rank>. Vendor — score/5.0\n\n"
            "## Vendor Profiles (one per vendor)\n"
            "**Strengths** (interpretation=strength only): criterion name — score/5.0 — one sentence why.\n"
            "**Gaps** (interpretation=gap or not_scored): criterion name — score/5.0 — what is missing.\n"
            "**Compliance summary**: X% FullyCompliant, Y% PartiallyCompliant — one sentence interpretation.\n\n"
            "## Decision Confidence\n"
            "State the confidence level and the score spread.\n\n"
            "## Recommendation\n"
            "Based on confidence level: either name the winner with justification, or state that scores "
            "are too close and human review is required."
        )

    messages = [{"role": "system", "content": _system_content}]

    if history:
        messages.extend(history[-6:])
    messages.append({"role": "user", "content": question})

    collected_hits: list[dict] = []
    tool_calls_used = 0

    # Build tools list dynamically based on callbacks passed.
    # In Stage A (RFP only) get_rfp_overview leads; in Stage B (with vendors) rankings leads.
    tools = []
    if get_rfp_overview is not None:
        tools.append(AGENT_RFP_OVERVIEW_TOOL)
    if get_vendor_rankings is not None:
        tools.append(AGENT_RANKINGS_TOOL)
    tools.append(AGENT_SEARCH_TOOL)
    if get_vendor_pricing is not None:
        tools.append(AGENT_PRICING_TOOL)
    if compare_vendors is not None:
        tools.append(AGENT_COMPARE_TOOL)
    if get_rfp_matrix_weights is not None:
        tools.append(AGENT_MATRIX_TOOL)

    # ── Tool-calling loop ─────────────────────────────────────────────────────
    while tool_calls_used < max_tool_calls:
        response = await groq.chat(
            messages,
            model=settings.GROQ_MAIN_MODEL,
            max_tokens=1500,
            temperature=0.2,
            tools=tools,
            tool_choice="auto",
        )
        message    = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []

        if not tool_calls:
            break

        assistant_message = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [],
        }
        for tc in tool_calls:
            assistant_message["tool_calls"].append({
                "id": tc.id, "type": tc.type,
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            })
        messages.append(assistant_message)

        for tc in tool_calls:
            tool_name = tc.function.name
            try:
                args = _parse_json(tc.function.arguments or "{}")
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}

            if tool_name == "search_database":
                query       = str(args.get("query", question)).strip() or question
                vendor_name = args.get("vendor_name")
                if not isinstance(vendor_name, str) or not vendor_name.strip():
                    vendor_name = None

                thought = (
                    f"Searching for '{query}'..."
                    if not vendor_name
                    else f"Searching {vendor_name}'s documents for '{query}'..."
                )
                yield {"type": "thought", "content": thought}

                result = await _call_search_database(search_database, query, vendor_name)
                collected_hits.extend(result["hits"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })
                tool_calls_used += 1

            elif tool_name == "get_vendor_pricing" and get_vendor_pricing is not None:
                vendor_name = str(args.get("vendor_name", "")).strip()
                yield {"type": "thought", "content": f"Retrieving pricing information for '{vendor_name}'..."}
                try:
                    result = await get_vendor_pricing(vendor_name)
                except Exception as exc:
                    logger.error("Error executing get_vendor_pricing: %s", exc)
                    result = {"error": str(exc)}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })
                tool_calls_used += 1

            elif tool_name == "compare_vendors" and compare_vendors is not None:
                req_id = str(args.get("requirement_id", "")).strip()
                v_a = str(args.get("vendor_a", "")).strip()
                v_b = str(args.get("vendor_b", "")).strip()
                yield {"type": "thought", "content": f"Comparing '{v_a}' and '{v_b}' on '{req_id}'..."}
                try:
                    result = await compare_vendors(req_id, v_a, v_b)
                except Exception as exc:
                    logger.error("Error executing compare_vendors: %s", exc)
                    result = {"error": str(exc)}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })
                tool_calls_used += 1

            elif tool_name == "get_rfp_matrix_weights" and get_rfp_matrix_weights is not None:
                yield {"type": "thought", "content": "Retrieving RFP evaluation matrix weights..."}
                try:
                    result = await get_rfp_matrix_weights()
                except Exception as exc:
                    logger.error("Error executing get_rfp_matrix_weights: %s", exc)
                    result = {"error": str(exc)}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })
                tool_calls_used += 1

            elif tool_name == "get_vendor_rankings" and get_vendor_rankings is not None:
                yield {"type": "thought", "content": "Retrieving official vendor scores and rankings..."}
                try:
                    result = await get_vendor_rankings()
                except Exception as exc:
                    logger.error("Error executing get_vendor_rankings: %s", exc)
                    result = {"error": str(exc)}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })
                tool_calls_used += 1

            elif tool_name == "get_rfp_overview" and get_rfp_overview is not None:
                yield {"type": "thought", "content": "Retrieving RFP structure and requirement overview..."}
                try:
                    result = await get_rfp_overview()
                except Exception as exc:
                    logger.error("Error executing get_rfp_overview: %s", exc)
                    result = {"error": str(exc)}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })
                tool_calls_used += 1

    # ── Build and yield citations BEFORE streaming tokens (ISSUE #18 FIX) ────
    seen: dict[str, dict] = {}
    for hit in collected_hits:
        hit_id = str(
            hit.get("id")
            or hit.get("metadata", {}).get("deep_link")
            or hit.get("text", "")
        )
        if hit_id not in seen or hit.get("score", 0) > seen[hit_id].get("score", 0):
            seen[hit_id] = hit

    citations = []
    for hit in sorted(seen.values(), key=lambda h: h.get("score", 0), reverse=True)[:6]:
        meta = hit.get("metadata", {})
        citations.append({
            "excerpt":        hit.get("text", "")[:280],
            "page_number":    meta.get("page_number", 1),
            "source":         meta.get("file_name", "Document"),
            "vendor_name":    meta.get("vendor_name", ""),
            "relevance_score": round(float(hit.get("score", 0.0)), 3),
            "deep_link":      meta.get("deep_link", "#"),
            "collection":     hit.get("collection", ""),
        })

    # Yield citations first so the frontend has them before tokens arrive
    yield {"type": "citations", "data": citations}

    # ── Stream the final answer tokens ────────────────────────────────────────
    async for token in groq.stream_chat(
        messages,
        model=settings.GROQ_MAIN_MODEL,
        max_tokens=1024,
        temperature=0.2,
    ):
        yield {"type": "token", "content": token}

    yield {"type": "done"}


# ── 7. Generic criterion scorer ───────────────────────────────────────────────
#
# Used for plan_methodology / experience_references / solution_quality / commercial.
# Input: criterion label + optional parent label + evaluation_mode + evidence.
# Output: score, confidence, compliance_level, justification.

_GENERIC_CRITERION_SYSTEM_PROMPT = """\
You are a senior RFP evaluator. Score how well the vendor's evidence satisfies \
a specific evaluation criterion.

YOU MUST follow this two-phase structure:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 1 — EVIDENCE REVIEW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[RELEVANCE] Does the evidence directly address this criterion or is it generic?
[QUALITY]   How specific, detailed, and concrete is the vendor's response?
[GAPS]      What is missing or only partially addressed?
[CONFIDENCE] How confident are you in this score given the evidence quality?

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 2 — VERDICT (JSON only, no extra text)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "reasoning": "<copy your Phase 1 analysis as a single string>",
  "score": <float 0.0–5.0>,
  "confidence": <float 0.0–1.0>,
  "compliance_level": "<FullyMet|MostlyMet|PartiallyMet|Weak|NotAddressed>",
  "justification": "<1-2 sentences citing the key evidence>"
}

Scoring scale:
  5.0 = FullyMet      — evidence is specific, complete, and clearly satisfies the criterion
  4.0 = MostlyMet     — strong evidence with minor gaps
  3.0 = PartiallyMet  — vendor addresses the criterion but important details are missing
  1.5 = Weak          — vague or boilerplate response; little substantive evidence
  0.0 = NotAddressed  — no relevant evidence found
"""

_TEAM_CAPABILITY_SYSTEM_PROMPT = """\
You are a senior RFP evaluator assessing the vendor's proposed team and resources \
for a criterion about staffing, experience, or team capability.

Evaluate the structured team summary AND any supporting evidence chunks provided.

YOU MUST follow this two-phase structure:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 1 — TEAM ASSESSMENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[COMPOSITION] Does the team have the right roles and seniority?
[EXPERIENCE]  Is the average experience appropriate? Are key roles senior enough?
[CERTIFICATIONS] Are relevant certifications/qualifications present?
[GAPS]        What is missing — roles, experience depth, certifications?
[CONFIDENCE]  How confident in this score given available data?

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 2 — VERDICT (JSON only, no extra text)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "reasoning": "<copy your Phase 1 analysis as a single string>",
  "score": <float 0.0–5.0>,
  "confidence": <float 0.0–1.0>,
  "compliance_level": "<FullyMet|MostlyMet|PartiallyMet|Weak|NotAddressed>",
  "justification": "<1-2 sentences citing specific team facts>"
}

Scoring scale:
  5.0 = FullyMet      — complete team, all key roles filled, strong seniority, relevant certs
  4.0 = MostlyMet     — good team with minor gaps in roles or certifications
  3.0 = PartiallyMet  — team is present but experience is thin or key roles are missing
  1.5 = Weak          — minimal team detail; only names/roles without experience data
  0.0 = NotAddressed  — no team information provided
"""

_GENERIC_SCORE_MAP = {
    "FullyMet":      5.0,
    "MostlyMet":     4.0,
    "PartiallyMet":  3.0,
    "Weak":          1.5,
    "NotAddressed":  0.0,
}


def _normalise_generic_result(parsed: dict, criterion_label: str) -> dict:
    level = str(parsed.get("compliance_level", "NotAddressed"))
    if level not in _GENERIC_SCORE_MAP:
        level = "NotAddressed"
    forced_score = _GENERIC_SCORE_MAP[level]
    return {
        "reasoning":        str(parsed.get("reasoning", "")),
        "score":            forced_score,
        "confidence":       min(1.0, max(0.0, float(parsed.get("confidence", 0.5)))),
        "compliance_level": level,
        "justification":    str(
            parsed.get("justification")
            or f"Criterion '{criterion_label}' scored as {level}."
        ),
    }


async def score_generic_criterion(
    criterion_label: str,
    parent_label: Optional[str],
    evaluation_mode: str,
    evidence_text: Optional[str],
    similarity_score: float,
) -> dict:
    """
    Score a single non-requirement criterion (plan_methodology,
    experience_references, solution_quality, commercial) against
    retrieved vendor evidence.

    Returns: reasoning, score (0–5), confidence (0–1),
             compliance_level, justification
    """
    groq = get_groq_client()

    if not evidence_text:
        return {
            "reasoning":        f"No vendor evidence found for '{criterion_label}'.",
            "score":            0.0,
            "confidence":       0.0,
            "compliance_level": "NotAddressed",
            "justification":    f"No evidence retrieved for criterion '{criterion_label}'.",
        }

    section_context = f" (under: {parent_label})" if parent_label else ""
    mode_hint = {
        "plan_methodology":      "Focus on how well the vendor describes their project plan, timeline, methodology, and deliverables.",
        "experience_references": "Focus on the quality, relevance, and depth of the vendor's references, portfolio, and similar project experience.",
        "solution_quality":      "Focus on the overall quality, clarity, completeness, and coherence of the vendor's proposal.",
        "commercial":            "Focus on the clarity, completeness, and competitiveness of the vendor's commercial/pricing proposal.",
    }.get(evaluation_mode, "Evaluate how well the vendor satisfies this criterion.")

    user_message = (
        f"CRITERION: {criterion_label}{section_context}\n"
        f"EVALUATION GUIDANCE: {mode_hint}\n\n"
        f"VENDOR EVIDENCE (semantic similarity: {similarity_score:.2f}):\n"
        f"{evidence_text[:7000]}\n\n"
        "Score using exactly one compliance_level: FullyMet (5), MostlyMet (4), "
        "PartiallyMet (3), Weak (1.5), or NotAddressed (0).\n"
        "Follow the two-phase structure. Output JSON verdict in Phase 2."
    )

    try:
        result = await groq.chat_text(
            [
                {"role": "system", "content": _GENERIC_CRITERION_SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
            model=settings.GROQ_MAIN_MODEL,
            max_tokens=1200,
            temperature=0,
        )
        parsed = _parse_json(result)
        return _normalise_generic_result(parsed, criterion_label)
    except Exception as exc:
        logger.warning("score_generic_criterion parse error for '%s': %s", criterion_label, exc)
        return {
            "reasoning":        f"Scoring failed for '{criterion_label}': {exc}",
            "score":            1.5,
            "confidence":       0.2,
            "compliance_level": "Weak",
            "justification":    f"Automated scoring failed for '{criterion_label}'; assigned minimum partial score.",
        }


async def score_team_capability_criterion(
    criterion_label: str,
    parent_label: Optional[str],
    team_summary: dict,
    evidence_text: Optional[str],
) -> dict:
    """
    Score a team_capability criterion using structured team data + evidence.

    team_summary should contain: member_count, roles, avg_years_experience,
    certifications, members (list of dicts from TeamMember table).
    """
    groq = get_groq_client()

    section_context = f" (under: {parent_label})" if parent_label else ""

    team_block = (
        f"Team size: {team_summary.get('member_count', 0)} members\n"
        f"Roles: {', '.join(team_summary.get('roles', [])) or 'not specified'}\n"
        f"Avg experience: {team_summary.get('avg_years_experience', 0):.1f} years\n"
        f"Certifications: {', '.join(team_summary.get('certifications', [])) or 'none listed'}\n"
    )
    members = team_summary.get("members", [])
    if members:
        team_block += "\nMember details:\n"
        for m in members[:8]:
            team_block += (
                f"  - {m.get('full_name', '?')} | {m.get('role', '?')} | "
                f"{m.get('years_experience', '?')} yrs | "
                f"certs: {', '.join(m.get('certifications', [])) or 'none'}\n"
            )

    evidence_block = (
        f"\nADDITIONAL EVIDENCE FROM PROPOSAL:\n{evidence_text[:3000]}"
        if evidence_text else ""
    )

    user_message = (
        f"CRITERION: {criterion_label}{section_context}\n\n"
        f"TEAM SUMMARY:\n{team_block}"
        f"{evidence_block}\n\n"
        "Score using exactly one compliance_level: FullyMet (5), MostlyMet (4), "
        "PartiallyMet (3), Weak (1.5), or NotAddressed (0).\n"
        "Follow the two-phase structure. Output JSON verdict in Phase 2."
    )

    try:
        result = await groq.chat_text(
            [
                {"role": "system", "content": _TEAM_CAPABILITY_SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
            model=settings.GROQ_MAIN_MODEL,
            max_tokens=1200,
            temperature=0,
        )
        parsed = _parse_json(result)
        return _normalise_generic_result(parsed, criterion_label)
    except Exception as exc:
        logger.warning("score_team_capability_criterion parse error for '%s': %s", criterion_label, exc)
        if team_summary.get("member_count", 0) > 0:
            return {
                "reasoning":        f"Scoring failed; team data available but LLM parse error: {exc}",
                "score":            2.0,
                "confidence":       0.3,
                "compliance_level": "PartiallyMet",
                "justification":    f"Team data available but automated scoring failed for '{criterion_label}'.",
            }
        return {
            "reasoning":        f"No team data and scoring failed: {exc}",
            "score":            0.0,
            "confidence":       0.0,
            "compliance_level": "NotAddressed",
            "justification":    f"No team data found for '{criterion_label}'.",
        }


# ── 8. Evaluation criteria builder (LLM-based, kept for legacy fallback) ──────
async def build_criteria_from_eval_sheet(sheet_content: str) -> list[dict]:
    """
    Parse a technical evaluation Excel sheet to extract criteria weights.
    Returns: [{label, section, weight, max_score}]
    """
    groq = get_groq_client()
    prompt = (
        f"Extract evaluation criteria and weights from this RFP evaluation sheet.\n\n"
        f"Content:\n{sheet_content[:2000]}\n\n"
        f"Return ONLY a JSON array: [{{\n"
        f'  "label": "criteria name",\n'
        f'  "section": "category/group",\n'
        f'  "weight": <0.0-1.0 as decimal>,\n'
        f'  "max_score": <integer, usually 5>\n'
        f"}}]\nWeights should sum to approximately 1.0."
    )
    result = await groq.chat_text(
        [{"role": "user", "content": prompt}],
        model=settings.GROQ_FAST_MODEL,
        max_tokens=800,
    )
    try:
        data = _parse_json(result)
        return data if isinstance(data, list) else []
    except Exception:
        return [
            {"label": "Functional",     "section": "Functional",     "weight": 0.35, "max_score": 5},
            {"label": "Technical",      "section": "Technical",      "weight": 0.35, "max_score": 5},
            {"label": "Security",       "section": "Security",       "weight": 0.10, "max_score": 5},
            {"label": "References",     "section": "References",     "weight": 0.15, "max_score": 5},
            {"label": "Infrastructure", "section": "Infrastructure", "weight": 0.05, "max_score": 5},
        ]


# ── 8. Chatbot Query Transformation ───────────────────────────────────────────
async def transform_query(query: str) -> list[str]:
    """
    Rewrite a query into 3 different semantic variations using the fast LLM.
    Returns a list of 3 strings (excluding the original query).
    """
    groq = get_groq_client()
    prompt = (
        f"You are a search query optimizer. Given the user's search query, "
        f"generate exactly 3 different semantic variations or search-engine-friendly reformulations "
        f"to maximize document retrieval chance.\n"
        f"Query: {query}\n\n"
        f"Respond with ONLY a JSON array of 3 strings, e.g. [\"variation 1\", \"variation 2\", \"variation 3\"]. "
        f"Do not include any extra text."
    )
    try:
        result = await groq.chat_text(
            [{"role": "user", "content": prompt}],
            model=settings.GROQ_FAST_MODEL,
            max_tokens=150,
            temperature=0.3,
        )
        parsed = _parse_json(result)
        if isinstance(parsed, list) and len(parsed) >= 3:
            return [str(q).strip() for q in parsed[:3]]
    except Exception as e:
        logger.warning("Query transformation failed: %s", e)
    
    # Fallback to simple variations if parsing/LLM fails
    return [
        f"{query} details",
        f"{query} specification",
        f"{query} implementation"
    ]


