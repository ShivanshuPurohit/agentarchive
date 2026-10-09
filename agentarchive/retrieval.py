"""Query terms and passage scoring shared by archive search and expansion."""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Sequence


WORDS = re.compile(r'"(?:[^"]|"")*"|[^\W_][\w./:-]*', re.UNICODE)
STOPWORDS = set("""
a an and are as at be been before being but by can could did do does doing each
every for from had has have how i if in into is it its latest me more most my of
on or our please previous previously prior should show some stable than that the
their them there these they this those through to up us versus vs was we were
what when where which who why will with would you your
""".split())


def terms(query: str) -> List[str]:
    if '"' not in query:
        query = re.sub(r"\bback(?:ing|ed)?\s+up\b", "backup", query, flags=re.I)
    found = [word.rstrip(".:/") for word in WORDS.findall(query)]
    signal = [word for word in found if word.casefold() not in STOPWORDS]
    return list(dict.fromkeys(signal or found))


def stem(term: str) -> str:
    word = term.casefold()
    if not word.isalpha():
        return word
    # Small, predictable inflection expansion. Identifiers and quoted phrases
    # remain exact; there are no domain-specific aliases or model calls.
    for suffix in ("ing", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[:-len(suffix)]
    return word


def atom(term: str) -> str:
    if term.startswith('"'):
        return term
    variants = forms(term)
    atoms = ['"' + word.replace('"', '""') + '"' for word in variants]
    return "(" + " OR ".join(atoms) + ")" if len(atoms) > 1 else atoms[0]


def forms(term: str) -> List[str]:
    word = term.casefold()
    if not word.isalpha() or len(word) < 4:
        return [word]
    base = stem(word)
    # Whole-word forms avoid prefix mistakes such as backing -> backlog or
    # stops -> stopwatch. Keep the user's exact spelling in every expansion.
    return list(dict.fromkeys([word, base, base + "s", base + "ed", base + "ing"]))


def passage_quality(item: Dict[str, Any], query_terms: Sequence[str], query: str = "") -> float:
    """Score the returned passage, not terms scattered across a huge block."""
    passage = str(item.get("excerpt") or "").casefold()
    plain = passage.replace("[", "").replace("]", "")
    matched = 0
    positions = []
    for term in query_terms:
        # FTS tokenizes punctuation in paths and identifiers. Match those same
        # components in the snippet, while keeping phrase order intact.
        variants = [term.strip('"').casefold()] if term.startswith('"') else forms(term)
        patterns = [r"[\W_]+".join(re.escape(piece) for piece in re.findall(r"[^\W_]+", word))
                    for word in variants]
        spans = [match.start() for pattern in patterns if pattern
                 for match in re.finditer(r"\b" + pattern + r"\b", plain)]
        if spans:
            matched += 1
            positions.extend((position, term) for position in spans)
    coverage = matched / max(1, len(query_terms))
    item["match_coverage"] = round(coverage, 3)
    prefix = str(item.get("_prefix") or "").casefold()
    evidence = (prefix + " " + plain).replace("\\", "")
    penalty = 0.0
    if len(query_terms) >= 3 and re.match(r"\s*(?:why|how|what|when|where|which|who)\b", query, re.I):
        # Repeated questions in old searches are not answer evidence. Keep
        # them retrievable, but do not let the echo suppress broader recall.
        question = " ".join(re.findall(r"\w+", query.casefold()))
        if question and question in " ".join(re.findall(r"\w+", evidence)):
            penalty += 0.75
    if re.search(r"\[(?:all-signal|nearby|broad|explicit) (?:claude|codex|pi)/", evidence) or (
        '"hit_id"' in evidence and '"excerpt"' in evidence
    ):
        penalty += 0.65  # A prior search response is a route to evidence.
    if re.search(r"agent-archive\s+(?:search|reasoning)\b", evidence):
        penalty += 0.30
    if "| query | hits |" in evidence or "bm25_score" in evidence:
        penalty += 0.35
    if any(marker in evidence for marker in ("expected_hit_ids", "approx_tokens_char4", '"answer_rank"')):
        penalty += 0.65
    if "indexed_context" in evidence or re.search(r"(?:^|\n)[irl]:(?:claude|codex|pi):\d+\s", evidence):
        penalty += 0.65
    if any(marker in prefix for marker in ("# agents.md", "<instructions>", "<recommended_plugins>")):
        penalty += 0.35
    size = int(item.get("_chars") or len(plain))
    penalty += min(0.65, 0.08 * math.log2(max(1, size / 4000)))
    kind = str(item.get("kind") or "")
    role = str(item.get("role") or "")
    native_answer = kind in {"output_text", "agent_message"} or (kind in {"text", "message"} and role == "assistant")
    direct_change = str(item.get("tool_name") or "").casefold() in {"edit", "write", "apply_patch"}
    # The actual change is at least as useful as an assistant describing it.
    bonus = 0.12 if native_answer or direct_change else 0.0
    nearby = max((len({term for other, term in positions if position <= other <= position + 120})
                  for position, _ in positions), default=0)
    bonus += 0.3 * nearby / max(1, len(query_terms))
    if '"root_cause"' in evidence and '"fix"' in evidence:
        bonus += 0.12
    if size < 80:
        penalty += 0.12
    if item.get("source") == "summary":
        penalty += 0.20
    # Rank is local to an index; raw BM25 values from different corpora are not
    # comparable. Rank only breaks close passage-quality ties.
    bonus += 0.08 / (1 + int(item.get("_rank") or 0) / 10)
    return coverage + bonus - penalty


def ranked_results(results: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    ordered = sorted(results, key=lambda row: (-float(row.get("_quality", 0)),
                     int(row.get("match_tier", 0)), int(row.get("_rank", 0))))
    unique = []
    hashes, excerpts, coordinates = set(), set(), set()
    for item in ordered:
        digest = item.get("text_sha256")
        excerpt = re.sub(r"\W+", " ", str(item.get("excerpt") or "").casefold()).strip()
        coordinate = item.get("hit_id") or (
            item.get("harness"), item.get("source"), item.get("session_id"), item.get("path"))
        if coordinate in coordinates or (digest and digest in hashes) or (excerpt and excerpt in excerpts):
            continue
        coordinates.add(coordinate)
        if digest:
            hashes.add(digest)
        if excerpt:
            excerpts.add(excerpt)
        unique.append(item)
    # Different passages from one investigation may be the best evidence.
    # Deduplicate the evidence itself; neither sessions nor harnesses get a quota.
    return unique[:limit]
