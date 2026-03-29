from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _read_text_with_fallback(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, "Unable to decode skill file")


@lru_cache(maxsize=1)
def load_novel_skill_text() -> str:
    enabled = os.getenv("NOVEL_SKILL_ENABLED", "1").strip()
    if enabled not in {"1", "true", "True", "YES", "yes"}:
        return ""

    skill_path = os.getenv("NOVEL_SKILL_PATH", "").strip()
    if not skill_path:
        return ""

    path = Path(skill_path)
    if not path.exists() or not path.is_file():
        return ""

    try:
        text = _read_text_with_fallback(path).strip()
    except Exception:
        return ""

    max_chars_raw = os.getenv("NOVEL_SKILL_MAX_CHARS", "8000").strip()
    try:
        max_chars = max(500, int(max_chars_raw))
    except Exception:
        max_chars = 8000

    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n[Skill content truncated]"
    return text


def build_skill_prompt_block() -> str:
    skill_text = load_novel_skill_text()
    if not skill_text:
        return ""
    return (
        "\n\n【外部小说创作Skill规则】\n"
        "以下规则来自你必须遵循的创作Skill，请在不违反JSON输出约束的前提下执行：\n"
        f"{skill_text}"
    )
