"""Safe, manifest-driven Season 1 audioletter importer.

The default path is a read-only dry run.  ``--apply`` is intentionally not
used by this module's CLI without an explicit confirmation flag in manage.py.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from db import transaction, utcnow


BUCKET_PREFIX = "audioletters/season1/"
BUCKET_KEY_RE = re.compile(r"^audioletters/season1/s1-(\d{2})-(\d{2})\.mp3$")
EPISODE_RE = re.compile(r"^## (S1-\d{2})\s*$", re.MULTILINE)
BLOCK_RE = re.compile(r"^### Block (\d+)\s*$", re.MULTILINE)
PRESERVED_REFERENCE_SEQUENCE = 7
PRESERVE_STATUS = "PRESERVE_EXISTING"


class ManifestValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ManifestBlock:
    sort_order: int
    block_type: str
    title: str
    content_code: str | None = None
    source_mp3: str | None = None
    bucket_key: str | None = None
    transcript: str | None = None
    body: str | None = None


@dataclass(frozen=True)
class ManifestEpisode:
    code: str
    sequence: int
    season: int
    season_episode: int
    title: str
    notion_page_title: str
    quiz_episode_code: str | None
    access_class: str
    is_published: bool
    blocks: tuple[ManifestBlock, ...]


def _field(section: str, name: str, *, required: bool = True) -> str | None:
    match = re.search(rf"^- {re.escape(name)}:\s*(.*)$", section, re.MULTILINE)
    if not match:
        if required:
            raise ManifestValidationError(f"Missing field '{name}'")
        return None
    return match.group(1)


def _fenced_field(section: str, name: str) -> str:
    match = re.search(
        rf"^- {re.escape(name)}:\s*\n\n```text\n([\s\S]*?)\n```",
        section,
        re.MULTILINE,
    )
    if not match:
        raise ManifestValidationError(f"Missing text body '{name}'")
    return match.group(1)


def _episode_sections(markdown: str) -> list[tuple[str, str]]:
    matches = list(EPISODE_RE.finditer(markdown))
    return [
        (match.group(1), markdown[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(markdown)])
        for index, match in enumerate(matches)
    ]


def parse_manifest(path: str | Path) -> tuple[ManifestEpisode, ...]:
    """Read the Final Manifest without transforming its titles or bodies."""
    markdown = Path(path).read_text(encoding="utf-8-sig")
    if "## Final Validation Summary" not in markdown:
        raise ManifestValidationError("Final Validation Summary is missing")
    summary = markdown.split("## Final Validation Summary", 1)[1]
    if not re.search(r"^- REVIEW_REQUIRED: 0\s*$", summary, re.MULTILINE):
        raise ManifestValidationError("Manifest is not migration-ready (REVIEW_REQUIRED must be 0)")

    episodes: list[ManifestEpisode] = []
    for code, section in _episode_sections(markdown.split("## Final Validation Summary", 1)[0]):
        episode_section, _, blocks_section = section.partition("blocks:\n")
        if not blocks_section:
            raise ManifestValidationError(f"{code}: blocks section is missing")
        raw_blocks = list(BLOCK_RE.finditer(blocks_section))
        blocks: list[ManifestBlock] = []
        for index, block_match in enumerate(raw_blocks):
            block_section = blocks_section[block_match.end(): raw_blocks[index + 1].start() if index + 1 < len(raw_blocks) else len(blocks_section)]
            sort_order = int(_field(block_section, "sort_order"))
            block_type = _field(block_section, "block_type")
            title = _field(block_section, "title")
            if block_type == "audio":
                blocks.append(ManifestBlock(
                    sort_order=sort_order,
                    block_type=block_type,
                    title=title,
                    content_code=_field(block_section, "content_code"),
                    source_mp3=_field(block_section, "source_mp3"),
                    bucket_key=_field(block_section, "bucket_key"),
                    transcript=_fenced_field(block_section, "transcript"),
                ))
            elif block_type == "info":
                blocks.append(ManifestBlock(
                    sort_order=sort_order,
                    block_type=block_type,
                    title=title,
                    body=_fenced_field(block_section, "body"),
                ))
            else:
                raise ManifestValidationError(f"{code}: unsupported block_type {block_type!r}")
        quiz = _field(episode_section, "quiz_episode_code")
        episodes.append(ManifestEpisode(
            code=code,
            sequence=int(_field(episode_section, "sequence")),
            season=int(_field(episode_section, "season")),
            season_episode=int(_field(episode_section, "season_episode")),
            title=_field(episode_section, "title"),
            notion_page_title=_field(episode_section, "notion_page_title"),
            quiz_episode_code=None if quiz == "null" else quiz,
            access_class=_field(episode_section, "access_class"),
            is_published=_field(episode_section, "is_published") == "true",
            blocks=tuple(blocks),
        ))
    validate_manifest(tuple(episodes))
    return tuple(episodes)


def validate_manifest(episodes: tuple[ManifestEpisode, ...]) -> None:
    expected_codes = [f"S1-{number:02d}" for number in range(1, 43)]
    if [episode.code for episode in episodes] != expected_codes:
        raise ManifestValidationError("Episodes must be exactly S1-01 through S1-42 in order")
    if len({episode.sequence for episode in episodes}) != 42:
        raise ManifestValidationError("Episode sequence is not unique")
    audio = [block for episode in episodes for block in episode.blocks if block.block_type == "audio"]
    if len(audio) != 86:
        raise ManifestValidationError(f"Expected 86 audio blocks, found {len(audio)}")
    source_mp3 = [block.source_mp3 for block in audio]
    bucket_keys = [block.bucket_key for block in audio]
    if None in source_mp3 or len(set(source_mp3)) != 86:
        raise ManifestValidationError("Audio source_mp3 values must be present and unique")
    if None in bucket_keys or len(set(bucket_keys)) != 86:
        raise ManifestValidationError("Audio bucket_key values must be present and unique")
    for episode in episodes:
        expected_access = "free" if episode.sequence <= 6 else "paid"
        if episode.access_class != expected_access:
            raise ManifestValidationError(f"{episode.code}: invalid access_class")
        if episode.is_published:
            raise ManifestValidationError(f"{episode.code}: is_published must be false")
        orders = [block.sort_order for block in episode.blocks]
        if orders != list(range(1, len(orders) + 1)):
            raise ManifestValidationError(f"{episode.code}: block sort_order must be continuous")
        expected_audio = 3 if episode.sequence in {1, 7} else 2
        if sum(block.block_type == "audio" for block in episode.blocks) != expected_audio:
            raise ManifestValidationError(f"{episode.code}: unexpected Audio count")
        for index, block in enumerate((b for b in episode.blocks if b.block_type == "audio"), 1):
            if not block.bucket_key or not block.bucket_key.startswith(BUCKET_PREFIX):
                raise ManifestValidationError(f"{episode.code}: invalid bucket prefix")
            match = BUCKET_KEY_RE.fullmatch(block.bucket_key)
            if not match or int(match.group(1)) != episode.sequence or int(match.group(2)) != index:
                raise ManifestValidationError(f"{episode.code}: invalid deterministic bucket key")
            if not block.transcript:
                raise ManifestValidationError(f"{episode.code}: Audio transcript is missing")
        for block in (b for b in episode.blocks if b.block_type == "info"):
            if block.body is None:
                raise ManifestValidationError(f"{episode.code}: Info body is missing")


def _readonly_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _db_blocks(conn: sqlite3.Connection, episode_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT sort_order,block_type,title,body,audio_storage_key,transcript "
        "FROM audioletter_blocks WHERE episode_id=? ORDER BY sort_order,id", (episode_id,)
    ).fetchall()


def _same_blocks(rows: list[sqlite3.Row], desired: ManifestEpisode) -> bool:
    if len(rows) != len(desired.blocks):
        return False
    for row, block in zip(rows, desired.blocks):
        if row["sort_order"] != block.sort_order or row["block_type"] != block.block_type or row["title"] != block.title:
            return False
        if block.block_type == "audio":
            if row["audio_storage_key"] != block.bucket_key or row["transcript"] != block.transcript or row["body"] != "":
                return False
        elif row["body"] != block.body or row["audio_storage_key"] is not None or row["transcript"] != "":
            return False
    return True


def _empty_draft(row: sqlite3.Row, blocks: list[sqlite3.Row]) -> bool:
    return not blocks and not row["audio_storage_key"] and not row["transcript"] and not row["is_published"]


def compare_episode(conn: sqlite3.Connection, desired: ManifestEpisode) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM audioletter_episodes WHERE sequence=?", (desired.sequence,)).fetchone()
    if row is None:
        return {"episode": "CREATE", "blocks": ["CREATE" for _ in desired.blocks], "reasons": []}
    # S1-07 is the independently verified Production reference episode.  Its
    # actual stored representation is intentionally authoritative for this
    # migration; do not compare it for reconciliation and never write it.
    if desired.sequence == PRESERVED_REFERENCE_SEQUENCE:
        return {
            "episode": PRESERVE_STATUS,
            "blocks": [PRESERVE_STATUS for _ in desired.blocks],
            "reasons": ["existing Production S1-07 reference episode is preserved by migration policy"],
        }
    blocks = _db_blocks(conn, row["id"])
    same_metadata = (
        row["season"] == desired.season and row["season_episode"] == desired.season_episode
        and row["title"] == desired.title and row["quiz_episode_code"] == desired.quiz_episode_code
        and bool(row["is_published"]) == desired.is_published
    )
    if same_metadata and _same_blocks(blocks, desired):
        return {"episode": "UNCHANGED", "blocks": ["UNCHANGED" for _ in desired.blocks], "reasons": []}
    if _empty_draft(row, blocks):
        return {"episode": "UPDATE", "blocks": ["CREATE" for _ in desired.blocks], "reasons": ["empty unpublished draft"]}
    reasons: list[str] = []
    if row["is_published"]:
        reasons.append("existing episode is published")
    if not same_metadata:
        reasons.append("episode metadata differs")
    if not _same_blocks(blocks, desired):
        reasons.append("existing blocks differ or use legacy representation")
    return {"episode": "CONFLICT", "blocks": ["CONFLICT" for _ in desired.blocks], "reasons": reasons}


def dry_run(db_path: str, manifest_path: str | Path) -> dict[str, Any]:
    episodes = parse_manifest(manifest_path)
    # SQLite mode=ro is intentional: dry-run cannot create, migrate, or modify a DB.
    with _readonly_connection(db_path) as conn:
        result_rows = []
        for episode in episodes:
            comparison = compare_episode(conn, episode)
            quiz_exists = (episode.quiz_episode_code is None or conn.execute(
                "SELECT 1 FROM episodes WHERE code=?", (episode.quiz_episode_code,)
            ).fetchone() is not None)
            if not quiz_exists:
                comparison = {"episode": "CONFLICT", "blocks": ["CONFLICT" for _ in episode.blocks],
                              "reasons": comparison["reasons"] + ["quiz_episode_code has no existing Quiz episode"]}
            result_rows.append({
                "code": episode.code,
                "sequence": episode.sequence,
                "episode": comparison["episode"],
                "blocks": comparison["blocks"],
                "quiz_episode_code": episode.quiz_episode_code,
                "audio_keys": [block.bucket_key for block in episode.blocks if block.block_type == "audio"],
                "reasons": comparison["reasons"],
            })
    counts = Counter(row["episode"] for row in result_rows)
    return {
        "mode": "dry-run",
        "manifest_episodes": len(episodes),
        "manifest_audio_blocks": sum(block.block_type == "audio" for episode in episodes for block in episode.blocks),
        "manifest_info_blocks": sum(block.block_type == "info" for episode in episodes for block in episode.blocks),
        "CREATE episodes": counts["CREATE"], "UPDATE episodes": counts["UPDATE"],
        "UNCHANGED episodes": counts["UNCHANGED"], "CONFLICT episodes": counts["CONFLICT"],
        "PRESERVE_EXISTING episodes": counts[PRESERVE_STATUS],
        "episodes": result_rows,
    }


def _insert_blocks(conn: sqlite3.Connection, episode_id: int, episode: ManifestEpisode, now: str) -> None:
    for block in episode.blocks:
        conn.execute(
            """INSERT INTO audioletter_blocks
               (episode_id,sort_order,block_type,title,body,audio_storage_key,transcript,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (episode_id, block.sort_order, block.block_type, block.title,
             "" if block.block_type == "audio" else block.body,
             block.bucket_key if block.block_type == "audio" else None,
             block.transcript if block.block_type == "audio" else "", now, now),
        )


def apply_manifest(db_path: str, manifest_path: str | Path) -> dict[str, Any]:
    """Future-only apply path.  It never deletes or overwrites populated rows."""
    episodes = parse_manifest(manifest_path)
    with transaction(db_path) as conn:
        comparisons = [compare_episode(conn, episode) for episode in episodes]
        missing_quiz = [episode.code for episode in episodes if episode.quiz_episode_code and not conn.execute(
            "SELECT 1 FROM episodes WHERE code=?", (episode.quiz_episode_code,)
        ).fetchone()]
        if missing_quiz or any(item["episode"] == "CONFLICT" for item in comparisons):
            raise ManifestValidationError("Apply blocked: existing content conflict or missing Quiz reference")
        created = updated = unchanged = preserved = 0
        for episode, comparison in zip(episodes, comparisons):
            if comparison["episode"] == PRESERVE_STATUS:
                # This branch intentionally performs no read-modify-write on
                # the episode or its blocks, including timestamps.
                preserved += 1
                continue
            if comparison["episode"] == "UNCHANGED":
                unchanged += 1
                continue
            now = utcnow()
            if comparison["episode"] == "CREATE":
                episode_id = conn.execute(
                    """INSERT INTO audioletter_episodes
                       (sequence,season,season_episode,title,audio_storage_key,transcript,quiz_episode_code,is_published,created_at,updated_at)
                       VALUES(?,?,?,?,NULL,'',?,0,?,?)""",
                    (episode.sequence, episode.season, episode.season_episode, episode.title,
                     episode.quiz_episode_code, now, now),
                ).lastrowid
                _insert_blocks(conn, episode_id, episode, now)
                created += 1
            else:  # only an empty unpublished draft can be UPDATE
                row = conn.execute("SELECT id FROM audioletter_episodes WHERE sequence=?", (episode.sequence,)).fetchone()
                conn.execute(
                    """UPDATE audioletter_episodes SET season=?,season_episode=?,title=?,quiz_episode_code=?,
                       audio_storage_key=NULL,transcript='',is_published=0,updated_at=? WHERE id=?""",
                    (episode.season, episode.season_episode, episode.title, episode.quiz_episode_code, now, row["id"]),
                )
                _insert_blocks(conn, row["id"], episode, now)
                updated += 1
    return {"mode": "apply", "created": created, "updated": updated,
            "unchanged": unchanged, "preserved": preserved}
