#!/usr/bin/env python3
"""
One-click WizNote to Obsidian migration and incremental sync.

What it does:
1. Logs in to WizNote cloud.
2. Downloads normal notes and collaboration notes.
3. Moves all note images into one central attachments folder.
4. Removes generated YAML properties and duplicated first H1 titles.
5. Tracks state for later incremental sync.

Requirements:
  The required WizNote downloader is vendored in vendor/wiznote_downloader.

Run:
  python3 wiznote_to_obsidian_one_click.py --user your@email.com

Optional:
  python3 wiznote_to_obsidian_one_click.py --output-base /path/to/output-base --clean
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString
from markdownify import markdownify as markdownify_html


DEFAULT_OUTPUT_BASE = Path.cwd()
DEFAULT_WIZ_TOOL_DIR = Path(__file__).resolve().parent / "vendor" / "wiznote_downloader"
IMAGE_LINK_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n+", re.DOTALL)
WIZ_GUID_RE = re.compile(
    r"docGuid:\s*([0-9a-fA-F-]{36})|note_guid:\s*([0-9a-fA-F-]{36})|documentGuid:\s*([0-9a-fA-F-]{36})"
)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}
STATE_FILE = ".wiznote_sync_state.json"


def require_wiz_tool(wiz_tool_dir: Path):
    downloader = wiz_tool_dir / "wiznote_downloader.py"
    if not downloader.exists():
        raise RuntimeError(
            f"Missing required WizNote migrator: {downloader}\n"
            "The vendored downloader is missing. Reinstall this project or pass --wiz-tool-dir."
        )
    sys.path.insert(0, str(wiz_tool_dir))
    import wiznote_downloader

    wiznote_downloader.parse_collaboration_note = robust_parse_collaboration_note
    wiznote_downloader.md = markdownify_preserving_complex_tables

    return wiznote_downloader.WizMigrator


def markdownify_preserving_complex_tables(html: str, **options) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    replacements: dict[str, str] = {}

    for index, table in enumerate(soup.find_all("table")):
        complex_cells = table.find_all(lambda tag: tag.name in {"td", "th"} and (tag.get("rowspan") or tag.get("colspan")))
        if not complex_cells:
            continue
        token = f"WIZ_COMPLEX_TABLE_{index}_PLACEHOLDER"
        replacements[token] = "\n\n" + str(table) + "\n\n"
        table.replace_with(NavigableString(token))

    markdown = markdownify_html(str(soup), **options)
    for token, table_html in replacements.items():
        markdown = markdown.replace(token, table_html)
    return markdown


def inline_markdown(text_arr: list[dict] | None) -> str:
    if not text_arr:
        return ""
    parts = []
    for item in text_arr:
        insert = str(item.get("insert", ""))
        attrs = item.get("attributes") or {}
        if attrs.get("link"):
            insert = f"[{insert}]({attrs.get('link')})"
        elif attrs.get("style-code"):
            insert = f"`{insert}`"
        elif attrs.get("style-bold"):
            insert = f"**{insert}**"
        elif attrs.get("style-italic"):
            insert = f"*{insert}*"
        elif attrs.get("style-strikethrough"):
            insert = f"~~{insert}~~"
        elif attrs.get("type") == "math":
            insert = f"${str(attrs.get('tex', '')).strip()}$"
        elif attrs.get("type") == "wiki-link":
            name = str(attrs.get("secondaryName") or attrs.get("name") or insert)
            name = name[:-3] if name.endswith(".md") else name
            insert = f"[[{name}]]"
        parts.append(insert)
    return "".join(parts)


def child_block_rows(data: dict, child_id: str) -> list[dict]:
    child = data.get(child_id, [])
    if isinstance(child, dict):
        if isinstance(child.get("blocks"), list):
            return child["blocks"]
        return [child]
    if isinstance(child, list):
        return [row for row in child if isinstance(row, dict)]
    return []


def table_has_spans(block: dict) -> bool:
    return any(str(key).endswith(("_rowSpan", "_colSpan")) and int(value or 1) > 1 for key, value in block.items())


def table_to_markdown(data: dict, block: dict) -> str:
    cols = int(block.get("cols") or 1)
    if table_has_spans(block):
        return spanned_table_to_markdown(data, block, cols)

    cells = []
    for child_id in block.get("children", []):
        rows = child_block_rows(data, child_id)
        cells.append(" ".join(inline_markdown(row.get("text")) for row in rows).strip())
    if not cells:
        return ""
    while len(cells) < cols:
        cells.append("")
    header = cells[:cols]
    body = cells[cols:]
    output = [
        markdown_table_row(header),
        markdown_table_row(["---"] * cols),
    ]
    for index in range(0, len(body), cols):
        row = body[index:index + cols]
        while len(row) < cols:
            row.append("")
        output.append(markdown_table_row(row))
    return "\n" + "\n".join(output) + "\n\n"


def spanned_table_to_markdown(data: dict, block: dict, cols: int) -> str:
    children = list(block.get("children", []))
    rows = int(block.get("rows") or 0)
    if not rows and cols:
        rows = max(1, (len(children) + cols - 1) // cols)

    grid = [["" for _ in range(cols)] for _ in range(rows)]
    occupied: set[tuple[int, int]] = set()
    child_index = 0

    for row_index in range(rows):
        for col_index in range(cols):
            if (row_index, col_index) in occupied:
                continue
            if child_index >= len(children):
                continue

            child_id = children[child_index]
            child_index += 1
            text = table_cell_plain_text(data, child_id)
            rowspan = int(block.get(f"{child_id}_rowSpan") or 1)
            colspan = int(block.get(f"{child_id}_colSpan") or 1)

            for r_offset in range(rowspan):
                for c_offset in range(colspan):
                    target_row = row_index + r_offset
                    target_col = col_index + c_offset
                    if target_row >= rows or target_col >= cols:
                        continue
                    grid[target_row][target_col] = text if c_offset == 0 else ""
                    if r_offset or c_offset:
                        occupied.add((target_row, target_col))

    return markdown_table_from_grid(grid)


def table_cell_plain_text(data: dict, child_id: str) -> str:
    rows = child_block_rows(data, child_id)
    parts = [inline_markdown(row.get("text")).strip() for row in rows]
    return "<br>".join(part for part in parts if part).replace("|", "\\|")


def markdown_table_from_grid(grid: list[list[str]]) -> str:
    if not grid:
        return ""
    cols = max(len(row) for row in grid)
    normalized = [row + [""] * (cols - len(row)) for row in grid]
    output = [
        markdown_table_row(normalized[0]),
        markdown_table_row(["---"] * cols),
    ]
    for row in normalized[1:]:
        output.append(markdown_table_row(row))
    return "\n" + "\n".join(output) + "\n\n"


def markdown_table_row(cells: list[str]) -> str:
    return "| " + " | ".join(cell if cell else " " for cell in cells) + " |"


def robust_block_to_markdown(data: dict, block: dict) -> str:
    block_type = block.get("type")
    text = inline_markdown(block.get("text"))

    if block.get("quoted"):
        return "\n".join(f"> {line}" for line in text.splitlines() or [""]) + "\n\n"

    if block_type == "text":
        if block.get("heading"):
            return f"\n{'#' * int(block.get('heading', 1))} {text}\n"
        return f"\n{text}\n" if text else "\n"

    if block_type == "list":
        level = int(block.get("level") or 1)
        indent = "  " * max(level - 1, 0)
        if block.get("ordered"):
            marker = f"{block.get('start') or 1}. "
        else:
            marker = "- "
            if block.get("checkbox") == "checked":
                marker += "[x] "
            elif block.get("checkbox") == "unchecked":
                marker += "[ ] "
        return f"{indent}{marker}{text}\n"

    if block_type == "code":
        language = block.get("language", "")
        lines = []
        for child_id in block.get("children", []):
            for row in child_block_rows(data, child_id):
                row_text = inline_markdown(row.get("text"))
                if row_text:
                    lines.append(row_text)
        return f"\n```{language}\n" + "\n".join(lines) + "\n```\n\n"

    if block_type == "table":
        return table_to_markdown(data, block)

    if block_type == "embed":
        embed_type = block.get("embedType", "")
        embed_data = block.get("embedData") or {}
        src = embed_data.get("src", "")
        file_name = embed_data.get("fileName") or Path(str(src)).name or embed_type
        if embed_type == "image":
            return f"\n![{file_name}]({src})\n\n"
        if embed_type == "hr":
            return "\n---\n\n"
        if embed_type == "toc":
            return "\n[TOC]\n\n"
        if embed_type == "mermaid":
            mermaid_text = embed_data.get("mermaidText")
            if mermaid_text:
                return f"\n```mermaid\n{mermaid_text}\n```\n\n"
        if embed_type in {"office", "drawio", "mermaid"} and src:
            return f"\n[{file_name}](wiz-collab-attachment://{src})\n\n"
        if embed_type == "webpage" and src:
            return f"\n[webpage]({src})\n\n"
        return ""

    if text:
        return f"\n{text}\n"
    return ""


def robust_parse_collaboration_note(origin_content: str) -> str | None:
    try:
        json_content = json.loads(origin_content)
        data = json_content.get("data", {}).get("data", {})
        blocks = data.get("blocks", [])
        content = "".join(robust_block_to_markdown(data, block) for block in blocks if isinstance(block, dict))
        content = re.sub(r"\n{4,}", "\n\n\n", content).strip()
        return content + "\n" if content else None
    except Exception as exc:
        print(f"    ⚠️  协作笔记兜底解析失败: {exc}")
        return None


def safe_name(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|\0]', "_", name).strip() or "asset"


def safe_note_name(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip() or "Untitled"


def rel_url(from_file: Path, target: Path) -> str:
    return Path(os.path.relpath(target, start=from_file.parent)).as_posix()


def is_remote(ref: str) -> bool:
    return urlparse(ref).scheme in {"http", "https", "data", "mailto"}


def unique_target(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    index = 2
    while True:
        candidate = target.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def read_doc_guid(md_text: str) -> str | None:
    match = WIZ_GUID_RE.search(md_text)
    if not match:
        return None
    return next(group for group in match.groups() if group)


def build_note_index(migrator, vault: Path) -> dict[Path, str]:
    index: dict[Path, str] = {}
    categories = migrator.get_all_categories()
    for category in categories:
        folder = category if isinstance(category, str) else category.get("key") or category.get("category") or category.get("path")
        if not folder:
            continue
        start = 0
        while True:
            url = f"{migrator.kapi_url}/ks/note/list/category/{migrator.kb_guid}"
            params = {"category": folder, "start": start, "count": 100, "with_abstract": 0, "order": "created-desc"}
            try:
                response = migrator.session.get(url, params=params, timeout=(migrator.connect_timeout, migrator.timeout))
                data = response.json()
                if data.get("returnCode", data.get("return_code")) != 200:
                    break
                notes = data.get("result", [])
            except Exception:
                break
            if not notes:
                break
            for note in notes:
                title = note.get("title") or note.get("documentTitle") or note.get("docTitle") or "Untitled"
                guid = note.get("docGuid") or note.get("guid") or note.get("documentGuid")
                if not guid:
                    continue
                md_path = vault / Path(str(folder).strip("/")) / f"{safe_note_name(str(title))}.md"
                index[md_path.resolve()] = str(guid)
            start += len(notes)
            if len(notes) < 100:
                break
    return index


def list_cloud_notes(migrator) -> list[dict]:
    notes: list[dict] = []
    categories = migrator.get_all_categories()
    seen: set[str] = set()
    for category in categories:
        folder = category if isinstance(category, str) else category.get("key") or category.get("category") or category.get("path")
        if not folder:
            continue
        start = 0
        while True:
            url = f"{migrator.kapi_url}/ks/note/list/category/{migrator.kb_guid}"
            params = {"category": folder, "start": start, "count": 100, "with_abstract": 0, "order": "created-desc"}
            try:
                response = migrator.session.get(url, params=params, timeout=(migrator.connect_timeout, migrator.timeout))
                data = response.json()
                if data.get("returnCode", data.get("return_code")) != 200:
                    break
                batch = data.get("result", [])
            except Exception as exc:
                print(f"  ! scan failed for {folder}: {exc}")
                break
            if not batch:
                break
            for note in batch:
                guid = note.get("docGuid") or note.get("guid") or note.get("documentGuid")
                if guid and guid not in seen:
                    seen.add(str(guid))
                    notes.append(note)
            start += len(batch)
            if len(batch) < 100:
                break
    return notes


def note_guid(note: dict) -> str:
    return str(note.get("docGuid") or note.get("guid") or note.get("documentGuid") or "")


def note_title(note: dict) -> str:
    return str(note.get("title") or note.get("documentTitle") or note.get("docTitle") or "Untitled")


def note_category(note: dict) -> str:
    return str(note.get("category") or note.get("documentCategory") or "/")


def note_signature(note: dict) -> dict:
    return {
        "version": note.get("version"),
        "dataModified": note.get("dataModified"),
        "infoModified": note.get("infoModified"),
        "dataMd5": note.get("dataMd5"),
        "infoMd5": note.get("infoMd5"),
        "title": note_title(note),
        "category": note_category(note),
        "type": note.get("type", note.get("documentType", "document")),
    }


def note_output_path(vault: Path, note: dict) -> Path:
    rel_dir = Path(note_category(note).strip("/"))
    return vault / rel_dir / f"{safe_note_name(note_title(note))}.md"


def load_state(vault: Path) -> dict:
    path = vault / STATE_FILE
    if not path.exists():
        return {"notes": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"notes": {}}


def save_state(vault: Path, notes: list[dict], succeeded_guids: set[str] | None = None) -> None:
    state_notes = {}
    for note in notes:
        guid = note_guid(note)
        if not guid:
            continue
        if succeeded_guids is not None and guid not in succeeded_guids:
            continue
        sig = note_signature(note)
        sig["path"] = str(note_output_path(vault, note).relative_to(vault))
        state_notes[guid] = sig
    payload = {"updatedAt": int(time.time()), "notes": state_notes}
    (vault / STATE_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_incremental_state(
    vault: Path,
    cloud_notes: list[dict],
    previous_state: dict,
    to_sync_guids: set[str],
    succeeded_guids: set[str],
) -> None:
    previous_notes = previous_state.get("notes", {})
    cloud_by_guid = {note_guid(note): note for note in cloud_notes if note_guid(note)}
    state_notes = {}

    for guid, note in cloud_by_guid.items():
        path = note_output_path(vault, note)
        if not path.exists():
            continue

        if guid in succeeded_guids:
            sig = note_signature(note)
            sig["path"] = str(path.relative_to(vault))
            state_notes[guid] = sig
        elif guid not in to_sync_guids and guid in previous_notes:
            state_notes[guid] = previous_notes[guid]
        elif guid in previous_notes:
            state_notes[guid] = previous_notes[guid]

    payload = {"updatedAt": int(time.time()), "notes": state_notes}
    (vault / STATE_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def changed_notes(vault: Path, cloud_notes: list[dict], state: dict, force_guids: set[str] | None = None, force_title: str | None = None) -> list[dict]:
    force_guids = force_guids or set()
    previous = state.get("notes", {})
    changed = []
    for note in cloud_notes:
        guid = note_guid(note)
        if not guid:
            continue
        if guid in force_guids or (force_title and force_title in note_title(note)):
            changed.append(note)
            continue
        sig = note_signature(note)
        old = previous.get(guid)
        expected_path = note_output_path(vault, note)
        comparable_keys = ["version", "dataModified", "infoModified", "dataMd5", "infoMd5", "title", "category", "type"]
        if not old or not expected_path.exists() or any(old.get(key) != sig.get(key) for key in comparable_keys):
            changed.append(note)
    return changed


def move_deleted_notes(vault: Path, cloud_notes: list[dict], state: dict) -> int:
    previous = state.get("notes", {})
    cloud_guids = {note_guid(note) for note in cloud_notes if note_guid(note)}
    deleted_dir = vault / "_deleted_from_wiznote"
    moved = 0
    for guid, old in previous.items():
        if guid in cloud_guids:
            continue
        rel_path = old.get("path")
        if not rel_path:
            continue
        source = vault / rel_path
        if not source.exists():
            continue
        target = unique_target(deleted_dir / rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved += 1
    return moved


def old_note_path(vault: Path, note: dict, state: dict) -> Path | None:
    old = state.get("notes", {}).get(note_guid(note), {})
    old_rel = old.get("path")
    if not old_rel:
        return None
    return vault / old_rel


def backup_existing_note(vault: Path, note: dict, state: dict) -> tuple[Path, Path] | None:
    path = old_note_path(vault, note, state) or note_output_path(vault, note)
    if not path.exists():
        return None
    backup_dir = vault / ".sync_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = unique_target(backup_dir / path.relative_to(vault))
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(backup))
    return backup, path


def restore_note_backup(backup_info: tuple[Path, Path] | None) -> None:
    if not backup_info:
        return
    backup, restore_path = backup_info
    if not backup.exists() or restore_path.exists():
        return
    restore_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(backup), str(restore_path))


def discard_note_backup(backup_info: tuple[Path, Path] | None) -> None:
    if not backup_info:
        return
    backup, _ = backup_info
    if backup.exists():
        backup.unlink()


def run_incremental_download(migrator, vault: Path, force_guids: set[str] | None = None, force_title: str | None = None) -> tuple[list[dict], set[str], set[str], dict]:
    print("\n== Scanning cloud notes for incremental sync ==")
    cloud_notes = list_cloud_notes(migrator)
    if not cloud_notes:
        raise RuntimeError("Cloud note scan returned 0 notes; aborting incremental sync to avoid moving local notes.")
    state = load_state(vault)
    to_sync = changed_notes(vault, cloud_notes, state, force_guids, force_title)
    to_sync_guids = {note_guid(note) for note in to_sync if note_guid(note)}
    moved_deleted = move_deleted_notes(vault, cloud_notes, state)
    print(f"cloud notes: {len(cloud_notes)}")
    print(f"changed/new notes: {len(to_sync)}")
    print(f"moved deleted notes: {moved_deleted}")

    succeeded: set[str] = set()
    vault.mkdir(parents=True, exist_ok=True)
    for index, note in enumerate(to_sync, 1):
        guid = note_guid(note)
        title = safe_note_name(note_title(note))
        output_path = note_output_path(vault, note)
        backup = backup_existing_note(vault, note, state)
        print(f"  [{index}/{len(to_sync)}] sync: {title}")
        result = migrator.process_note(note, str(vault))
        if result and result.get("status") in {"success", "skip"}:
            old_path = old_note_path(vault, note, state)
            if old_path and old_path.exists() and old_path.resolve() != output_path.resolve():
                old_path.unlink()
            succeeded.add(guid)
            discard_note_backup(backup)
        elif result:
            restore_note_backup(backup)
            print(f"    ! {result.get('status')}: {result.get('error') or result.get('message') or 'not fully synced'}")
        else:
            restore_note_backup(backup)
    return cloud_notes, to_sync_guids, succeeded, state


def download_collab_asset(migrator, doc_guid: str, filename: str, target: Path) -> bool:
    token = migrator.get_collaboration_token(doc_guid)
    if not token:
        return False
    ws_domain = urlparse(migrator.kapi_url).netloc
    url = f"https://{ws_domain}/editor/{migrator.kb_guid}/{doc_guid}/resources/{filename}"
    headers = {"Cookie": f"x-live-editor-token={token}"}
    for _ in range(2):
        try:
            response = migrator.session.get(url, headers=headers, timeout=(8, 20), stream=True)
            if response.status_code != 200:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        handle.write(chunk)
            return True
        except Exception:
            continue
    return False


def centralize_images(vault: Path, migrator, note_index: dict[Path, str] | None = None) -> dict[str, int]:
    attachments = vault / "attachments"
    attachments.mkdir(parents=True, exist_ok=True)
    if note_index is None:
        note_index = build_note_index(migrator, vault)
    stats = {"notes": 0, "links": 0, "moved": 0, "downloaded": 0, "missing": 0, "rewritten": 0}

    for md in sorted(vault.rglob("*.md")):
        if attachments in md.parents:
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        matches = list(IMAGE_LINK_RE.finditer(text))
        if not matches:
            continue
        stats["notes"] += 1
        doc_guid = read_doc_guid(text) or note_index.get(md.resolve())
        replacements: dict[str, str] = {}

        for match in matches:
            alt, ref = match.group(1), match.group(2).strip()
            stats["links"] += 1
            if is_remote(ref):
                continue
            clean_ref = ref.split("#", 1)[0].split("?", 1)[0]
            source = (md.parent / clean_ref).resolve()
            filename = safe_name(Path(clean_ref).name)
            if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
                filename += ".png"
            target = attachments / filename

            if source.exists() and (source == target.resolve() or attachments in source.parents):
                replacements[match.group(0)] = f"![{alt}]({rel_url(md, source)})"
                continue

            target = unique_target(target)
            if source.exists():
                shutil.move(str(source), str(target))
                stats["moved"] += 1
            elif doc_guid and download_collab_asset(migrator, doc_guid, Path(clean_ref).name, target):
                stats["downloaded"] += 1
            else:
                stats["missing"] += 1
                continue

            replacements[match.group(0)] = f"![{alt}]({rel_url(md, target)})"

        if replacements:
            new_text = text
            for old, new in replacements.items():
                new_text = new_text.replace(old, new)
            if new_text != text:
                stats["rewritten"] += 1
                md.write_text(new_text, encoding="utf-8")

    remove_empty_asset_dirs(vault, attachments)
    return stats


def remove_empty_asset_dirs(vault: Path, attachments: Path) -> None:
    for path in sorted(vault.rglob("*_files"), key=lambda p: len(p.parts), reverse=True):
        if path == attachments or attachments in path.parents:
            continue
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def cleanup_markdown(vault: Path) -> int:
    changed_count = 0
    for md in sorted(vault.rglob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        new_text = FRONTMATTER_RE.sub("", text, count=1).lstrip("\n")
        lines = new_text.splitlines()
        index = 0
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index < len(lines):
            match = re.match(r"^#\s+(.+?)\s*$", lines[index])
            if match and normalize_title(match.group(1)) == normalize_title(md.stem):
                del lines[index]
                while index < len(lines) and not lines[index].strip():
                    del lines[index]
                new_text = "\n".join(lines).lstrip("\n")
                if new_text:
                    new_text += "\n"
        if new_text != text:
            md.write_text(new_text, encoding="utf-8")
            changed_count += 1
    return changed_count


def validate_images(vault: Path) -> int:
    missing = 0
    for md in vault.rglob("*.md"):
        text = md.read_text(encoding="utf-8", errors="replace")
        for ref in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
            if is_remote(ref):
                continue
            if not (md.parent / ref).resolve().exists():
                missing += 1
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description="One-click WizNote to Obsidian migration and incremental sync.")
    parser.add_argument("--user", default=os.environ.get("WIZNOTE_USER"), help="WizNote account; can also use WIZNOTE_USER")
    parser.add_argument("--password", default=os.environ.get("WIZNOTE_PASSWORD"), help="WizNote password; can also use WIZNOTE_PASSWORD")
    parser.add_argument(
        "--wiz-tool-dir",
        type=Path,
        default=Path(os.environ.get("WIZNOTE_TOOL_DIR")) if os.environ.get("WIZNOTE_TOOL_DIR") else DEFAULT_WIZ_TOOL_DIR,
        help="Directory containing wiznote_downloader.py; defaults to the vendored downloader; can also use WIZNOTE_TOOL_DIR",
    )
    parser.add_argument("--output-base", type=Path, default=DEFAULT_OUTPUT_BASE, help="Directory that will contain wiznote_download")
    parser.add_argument("--clean", action="store_true", help="Delete existing wiznote_download before running a full rebuild")
    parser.add_argument("--full", action="store_true", help="Full rebuild without deleting output first; use --clean for a clean full rebuild")
    parser.add_argument("--force-guid", action="append", default=[], help="Force sync a specific WizNote docGuid; can be repeated")
    parser.add_argument("--force-title", help="Force sync notes whose title contains this text")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    if not args.user:
        parser.error("--user is required unless WIZNOTE_USER is set")
    WizMigrator = require_wiz_tool(args.wiz_tool_dir.resolve())
    output_base = args.output_base.resolve()
    vault = output_base / "wiznote_download"

    full_rebuild = args.clean or args.full
    if args.clean and vault.exists():
        shutil.rmtree(vault)
    output_base.mkdir(parents=True, exist_ok=True)

    password = args.password or getpass.getpass("WizNote password: ")
    migrator = WizMigrator(args.user, password, max_workers=args.workers, timeout=45, connect_timeout=15)
    ok, error = migrator.login()
    if not ok:
        raise RuntimeError(error or "WizNote login failed")

    cloud_notes: list[dict] | None = None
    succeeded_guids: set[str] | None = None
    to_sync_guids: set[str] | None = None
    previous_state: dict | None = None

    if full_rebuild:
        old_cwd = Path.cwd()
        try:
            os.chdir(output_base)
            # The bundled full migrator performs its own login and progress output.
            migrator.run()
        finally:
            os.chdir(old_cwd)
        cloud_notes = list_cloud_notes(migrator)
        succeeded_guids = {note_guid(note) for note in cloud_notes if note_output_path(vault, note).exists()}
    else:
        cloud_notes, to_sync_guids, succeeded_guids, previous_state = run_incremental_download(migrator, vault, set(args.force_guid), args.force_title)

    print("\n== Centralizing images ==")
    note_index = {note_output_path(vault, note).resolve(): note_guid(note) for note in cloud_notes if note_guid(note)}
    image_stats = centralize_images(vault, migrator, note_index)
    for key, value in image_stats.items():
        print(f"{key}: {value}")

    print("\n== Cleaning Markdown ==")
    changed = cleanup_markdown(vault)
    print(f"cleaned notes: {changed}")

    if cloud_notes is not None:
        if full_rebuild:
            existing_guids = {note_guid(note) for note in cloud_notes if note_guid(note) and note_output_path(vault, note).exists()}
            save_state(vault, cloud_notes, existing_guids)
        else:
            save_incremental_state(vault, cloud_notes, previous_state or {"notes": {}}, to_sync_guids or set(), succeeded_guids or set())
        print(f"sync state: {vault / STATE_FILE}")

    print("\n== Validation ==")
    missing = validate_images(vault)
    print(f"vault: {vault}")
    print(f"attachments: {vault / 'attachments'}")
    print(f"missing image links: {missing}")
    if missing:
        print("Some remaining image links point to old local file:// paths that are not available in WizNote cloud.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
