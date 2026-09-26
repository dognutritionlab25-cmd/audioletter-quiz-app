"""Read-only-first maintenance scan for Season 1 Audioletter text residue.

The scanner deliberately works from the current database, never the migration
manifest.  It only proposes mechanical serialization cleanup; it does not
rewrite content, change ordering, or touch audio storage keys.
"""
from __future__ import annotations

import re
import sqlite3
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from db import transaction, utcnow


SEASON = 1
SAFE_AUTO_FIX = "SAFE_AUTO_FIX"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
PRESERVED_MARKDOWN = "PRESERVED_MARKDOWN"

ENCODED_BR_RE = re.compile(r"&lt;\s*br\s*/?\s*&gt;", re.IGNORECASE)
LITERAL_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
ENCODED_EMPTY_BLOCK_RE = re.compile(r"&lt;\s*empty-block\s*&gt;", re.IGNORECASE)
LITERAL_EMPTY_BLOCK_RE = re.compile(r"<\s*empty-block\s*>", re.IGNORECASE)
SYNCED_BLOCK_RE = re.compile(r"<\s*/?\s*synced_block(?:\s+[^<>]*)?\s*>", re.IGNORECASE)
HTML_LIKE_RE = re.compile(r"(?:<|&lt;)\s*/?\s*[a-z][\w:-]*(?:\s+[^<>]*?)?(?:>|&gt;)", re.IGNORECASE)
MARKDOWN_EMPHASIS_RE = re.compile(r"(?<!\\)(?:\*\*\*?|__?)(?=\S)")
CODE_LIKE_RE = re.compile(
    r"(?im)^\s*(?:```|def\s|class\s|SELECT\s|INSERT\s|UPDATE\s|DELETE\s|\{|\}|//|#include|\||[-=]{3,})"
)
INDENTED_LIST_RE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|>)")
CODE_SYMBOL_RE = re.compile(r"(?:=>|==|!=|\{.*\}|;|\|)")


class CleanupValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CleanupFinding:
    episode_code: str
    episode_id: int
    block_id: int | None
    sort_order: int | None
    block_type: str | None
    table: str
    field: str
    patterns: tuple[str, ...]
    safe_patterns: tuple[str, ...]
    review_patterns: tuple[str, ...]
    classification: str
    before: str
    after: str | None
    note: str


def _readonly_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _preview(value: str, limit: int = 160) -> str:
    compact = value.replace("\n", "\\n").replace("\t", "\\t")
    return compact if len(compact) <= limit else f"{compact[:limit - 1]}…"


def _standalone_matches(value: str, pattern: re.Pattern[str]) -> bool:
    """Only remove tag-like residues that occupy an otherwise empty line."""
    matches = list(pattern.finditer(value))
    if not matches:
        return True
    for match in matches:
        line_start = value.rfind("\n", 0, match.start()) + 1
        line_end = value.find("\n", match.end())
        if line_end == -1:
            line_end = len(value)
        if value[line_start:match.start()].strip() or value[match.end():line_end].strip():
            return False
    return True


def _remove_standalone_lines(value: str, pattern: re.Pattern[str], replacement: str) -> str:
    return re.sub(
        rf"(?im)^[ \t]*(?:{pattern.pattern})[ \t]*(?:\n|$)",
        replacement,
        value,
    )


def _prose_indentation_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped and not INDENTED_LIST_RE.match(line) and not CODE_SYMBOL_RE.search(stripped))


def _indentation_action(value: str) -> tuple[str | None, str | None]:
    """Normalize repeated prose indentation, but preserve possibly meaningful layout."""
    lines = value.splitlines(keepends=True)
    content = [line for line in lines if line.strip()]
    if not content:
        return None, None
    has_tab = any(line.startswith("\t") for line in content)
    has_spaces = any(line.startswith("    ") for line in content)
    if not has_tab and not has_spaces:
        return None, None
    indented = [line for line in content if line.startswith("\t") or line.startswith("    ")]
    if CODE_LIKE_RE.search(value) or any(not _prose_indentation_line(line) for line in indented):
        return "AMBIGUOUS_INDENTATION", None
    # A single indented line has no reliable evidence that it is export residue.
    if len(indented) < 2:
        return "AMBIGUOUS_INDENTATION", None
    if has_tab and not has_spaces and all(line.startswith("\t") for line in indented):
        return "TAB_INDENTATION", "".join(line[1:] if line.startswith("\t") else line for line in lines)
    if not has_tab and all(line.startswith("    ") for line in indented):
        return "FOUR_SPACE_INDENTATION", "".join(
            line[4:] if line.startswith("    ") else line for line in lines
        )
    return "AMBIGUOUS_INDENTATION", None


def _inspect_field(*, episode: sqlite3.Row, block: sqlite3.Row | None, table: str, field: str, value: str) -> CleanupFinding | None:
    if not value:
        return None
    patterns: list[str] = []
    safe_patterns: list[str] = []
    review_patterns: list[str] = []
    candidate = value
    review_notes: list[str] = []

    if ENCODED_BR_RE.search(candidate):
        patterns.append("ENCODED_BR")
        safe_patterns.append("ENCODED_BR")
        candidate = ENCODED_BR_RE.sub("\n", candidate)
    if LITERAL_BR_RE.search(candidate):
        patterns.append("LITERAL_BR")
        safe_patterns.append("LITERAL_BR")
        candidate = LITERAL_BR_RE.sub("\n", candidate)

    for label, pattern in (("ENCODED_EMPTY_BLOCK", ENCODED_EMPTY_BLOCK_RE), ("LITERAL_EMPTY_BLOCK", LITERAL_EMPTY_BLOCK_RE)):
        if pattern.search(candidate):
            patterns.append(label)
            if _standalone_matches(candidate, pattern):
                safe_patterns.append(label)
                candidate = _remove_standalone_lines(candidate, pattern, "\n")
            else:
                review_notes.append(f"{label} is not a standalone empty line")
                review_patterns.append(label)

    if SYNCED_BLOCK_RE.search(candidate):
        patterns.append("NOTION_SYNCED_BLOCK_WRAPPER")
        if _standalone_matches(candidate, SYNCED_BLOCK_RE):
            safe_patterns.append("NOTION_SYNCED_BLOCK_WRAPPER")
            candidate = _remove_standalone_lines(candidate, SYNCED_BLOCK_RE, "")
        else:
            review_notes.append("Notion synced_block wrapper is not standalone")
            review_patterns.append("NOTION_SYNCED_BLOCK_WRAPPER")

    indent_pattern, normalized = _indentation_action(candidate)
    if indent_pattern:
        patterns.append(indent_pattern)
        if normalized is None:
            review_notes.append("indentation may be intentional preformatted content")
            review_patterns.append(indent_pattern)
        else:
            safe_patterns.append(indent_pattern)
            candidate = normalized

    # Any unknown tag-like residue is intentionally never removed automatically.
    known_tags = (ENCODED_BR_RE, LITERAL_BR_RE, ENCODED_EMPTY_BLOCK_RE,
                  LITERAL_EMPTY_BLOCK_RE, SYNCED_BLOCK_RE)
    if HTML_LIKE_RE.search(candidate) and not any(pattern.search(candidate) for pattern in known_tags):
        patterns.append("UNKNOWN_HTML_OR_EXPORT_ARTIFACT")
        review_notes.append("unknown HTML-like or export residue")
        review_patterns.append("UNKNOWN_HTML_OR_EXPORT_ARTIFACT")

    markdown_present = bool(MARKDOWN_EMPHASIS_RE.search(candidate))
    if markdown_present:
        patterns.append("MARKDOWN_EMPHASIS")

    episode_code = f"S1-{episode['sequence']:02d}"
    identity = dict(
        episode_code=episode_code,
        episode_id=episode["id"],
        block_id=block["id"] if block else None,
        sort_order=block["sort_order"] if block else None,
        block_type=block["block_type"] if block else None,
        table=table,
        field=field,
    )
    if review_notes:
        return CleanupFinding(**identity, patterns=tuple(patterns), classification=REVIEW_REQUIRED,
                              safe_patterns=tuple(safe_patterns), review_patterns=tuple(review_patterns),
                              before=value, after=candidate if candidate != value else None,
                              note="; ".join(review_notes))
    if candidate != value:
        return CleanupFinding(**identity, patterns=tuple(patterns), classification=SAFE_AUTO_FIX,
                              safe_patterns=tuple(safe_patterns), review_patterns=(),
                              before=value, after=candidate,
                              note="mechanical serialization residue cleanup")
    if markdown_present:
        return CleanupFinding(**identity, patterns=tuple(patterns), classification=PRESERVED_MARKDOWN,
                              safe_patterns=(), review_patterns=(),
                              before=value, after=None,
                              note="valid Markdown emphasis is preserved; no cleanup proposed")
    return None


def _scan_connection(conn: sqlite3.Connection) -> tuple[dict[str, int], list[CleanupFinding]]:
    episodes = conn.execute(
        "SELECT id,sequence,title,transcript FROM audioletter_episodes WHERE season=? ORDER BY sequence,id", (SEASON,)
    ).fetchall()
    findings: list[CleanupFinding] = []
    scanned_blocks = scanned_fields = 0
    for episode in episodes:
        block_rows = conn.execute(
            "SELECT id,sort_order,block_type,title,body,transcript,audio_storage_key "
            "FROM audioletter_blocks WHERE episode_id=? ORDER BY sort_order,id", (episode["id"],)
        ).fetchall()
        for field, value in (("title", episode["title"]),):
            scanned_fields += 1
            finding = _inspect_field(episode=episode, block=None, table="audioletter_episodes", field=field, value=value)
            if finding:
                findings.append(finding)
        # The legacy transcript is only rendered if canonical blocks do not exist.
        if not block_rows and episode["transcript"]:
            scanned_fields += 1
            finding = _inspect_field(episode=episode, block=None, table="audioletter_episodes",
                                     field="transcript", value=episode["transcript"])
            if finding:
                findings.append(finding)
        for block in block_rows:
            scanned_blocks += 1
            fields = [("title", block["title"])]
            fields.append(("transcript", block["transcript"]) if block["block_type"] == "audio" else ("body", block["body"]))
            for field, value in fields:
                scanned_fields += 1
                finding = _inspect_field(episode=episode, block=block, table="audioletter_blocks", field=field, value=value)
                if finding:
                    findings.append(finding)
    return {"scanned episodes": len(episodes), "scanned blocks": scanned_blocks, "scanned fields": scanned_fields}, findings


def _report(summary: dict[str, int], findings: list[CleanupFinding], *, mode: str,
            include_full_values: bool = False) -> dict[str, Any]:
    pattern_counts = Counter(pattern for finding in findings for pattern in finding.patterns)
    status_counts = Counter(finding.classification for finding in findings)
    affected_episodes = sorted({finding.episode_code for finding in findings})
    affected_blocks = {(finding.episode_id, finding.block_id) for finding in findings if finding.block_id is not None}
    report = {
        "mode": mode,
        **summary,
        "affected episodes": len(affected_episodes),
        "affected blocks": len(affected_blocks),
        "pattern counts": dict(sorted(pattern_counts.items())),
        "SAFE_AUTO_FIX": status_counts[SAFE_AUTO_FIX],
        "REVIEW_REQUIRED": status_counts[REVIEW_REQUIRED],
        "PRESERVED_MARKDOWN": status_counts[PRESERVED_MARKDOWN],
        "summary": {
            "mode": mode,
            "scanned_episodes": summary["scanned episodes"],
            "scanned_blocks": summary["scanned blocks"],
            "scanned_fields": summary["scanned fields"],
            "affected_episodes": len(affected_episodes),
            "affected_blocks": len(affected_blocks),
            "pattern_counts": dict(sorted(pattern_counts.items())),
            "SAFE_AUTO_FIX": status_counts[SAFE_AUTO_FIX],
            "REVIEW_REQUIRED": status_counts[REVIEW_REQUIRED],
            "PRESERVED_MARKDOWN": status_counts[PRESERVED_MARKDOWN],
        },
        "findings": [
            {
                "episode": item.episode_code, "episode_sequence": int(item.episode_code[3:]),
                "sort_order": item.sort_order, "block_type": item.block_type,
                "field": item.field, "detected_patterns": list(item.patterns),
                "safe_patterns": list(item.safe_patterns), "review_patterns": list(item.review_patterns),
                "classification": item.classification, "before_preview": _preview(item.before),
                "proposed_after_preview": _preview(item.after) if item.after is not None else None,
                "note": item.note,
            }
            for item in findings
        ],
    }
    if include_full_values:
        for item, rendered in zip(findings, report["findings"]):
            rendered["before_value"] = item.before
            rendered["proposed_after_value"] = item.after
    return report


def scan_cleanup(db_path: str, *, report_json: str | Path | None = None) -> dict[str, Any]:
    """Read-only report. SQLite mode=ro guarantees the DB cannot be changed."""
    with _readonly_connection(db_path) as conn:
        summary, findings = _scan_connection(conn)
    console_report = _report(summary, findings, mode="dry-run")
    if report_json is not None:
        report_path = Path(report_json)
        report_path.write_text(
            json.dumps(_report(summary, findings, mode="dry-run", include_full_values=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console_report["report_json"] = str(report_path)
    return console_report


def _update_finding(conn: sqlite3.Connection, finding: CleanupFinding) -> None:
    if finding.after is None:
        return
    if finding.table == "audioletter_episodes":
        conn.execute(
            f"UPDATE audioletter_episodes SET {finding.field}=?,updated_at=? WHERE id=? AND {finding.field}=?",
            (finding.after, utcnow(), finding.episode_id, finding.before),
        )
    elif finding.table == "audioletter_blocks" and finding.block_id is not None:
        conn.execute(
            f"UPDATE audioletter_blocks SET {finding.field}=?,updated_at=? WHERE id=? AND episode_id=? AND {finding.field}=?",
            (finding.after, utcnow(), finding.block_id, finding.episode_id, finding.before),
        )
    else:
        raise CleanupValidationError("unsupported cleanup target")


def apply_cleanup(db_path: str) -> dict[str, Any]:
    """Future-only transactional apply. REVIEW_REQUIRED always blocks writes."""
    with transaction(db_path) as conn:
        summary, findings = _scan_connection(conn)
        review = [item for item in findings if item.classification == REVIEW_REQUIRED]
        if review:
            raise CleanupValidationError("REVIEW_REQUIRED findings block cleanup apply")
        safe = [item for item in findings if item.classification == SAFE_AUTO_FIX]
        for finding in safe:
            _update_finding(conn, finding)
    result = _report(summary, findings, mode="apply")
    result["updated fields"] = len(safe)
    return result
