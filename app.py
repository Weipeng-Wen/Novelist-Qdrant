from pathlib import Path
import traceback
import random
import re
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from agent.agent import run_continue_chain, run_create_chain

# 路径配置
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
SQLITE_DB_PATH = BASE_DIR / "data" / "novel.db"

# 创建 FastAPI 应用实例
app = FastAPI(title="Novelist Agent Web")

# 配置一个静态文件服务路由（将指定路径中的静态文件放入该路由），而不需要为每个文件单独编写路由
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

# 定义一个函数来生成 HTTP 响应头，指示浏览器不要缓存响应内容。这对于动态内容或频繁更新的资源特别有用。
def _nocache_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }

# 定义一个函数来安全地将输入转换为整数，如果转换失败则返回一个默认值。
def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default

# 定义一个函数来计算中文文本的字数，方法是去除所有空白字符后计算剩余字符的长度。
def _word_count_cn(text: str) -> int:
    # “\s+”是多次匹配任何空白字符（包括空格、制表符、换行符等）的正则表达式模式。
    # re.sub()是正则化替换函数，不用strip是因为他只能删除首尾空格，不用replace是因为他一次只能替换一种
    return len(re.sub(r"\s+", "", text or ""))

# 定义一个函数来生成安全的文件名，去除非法字符并确保文件名不为空。
def _safe_filename(name: str) -> str:
    candidate = str(name or "").strip()
    if not candidate:
        candidate = "未命名小说"
    candidate = re.sub(r'[\\/:*?"<>|]+', "_", candidate)
    candidate = candidate.strip().strip(".")
    return candidate or "未命名小说"

# 定义一个函数来构建小说内容的纯文本格式，包含标题、简介和章节内容。
def _build_export_text(bundle: dict) -> str:
    title = str(bundle.get("title", "")).strip() or f"小说_{bundle.get('novel_id', '')}"
    intro = str(bundle.get("intro", "") or "").strip()
    chapters = bundle.get("chapters", []) or []

    # 多几个“”是为了在.join时能有更明显的段落分隔，提升可读性
    parts: list[str] = [title, "", "【文章简介】", intro or "暂无简介", ""]

    for chapter in chapters:
        chapter_id = int(chapter.get("chapter_id", 0))
        chapter_title = str(chapter.get("chapter_title", "") or chapter.get("title", "")).strip()
        chapter_text = str(chapter.get("chapter_full_text", "") or "").strip()
        heading = f"第{chapter_id}章"
        if chapter_title:
            heading = f"{heading} {chapter_title}"
        parts.append(heading)
        parts.append(chapter_text or "（本章暂无正文）")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"

# 定义一个函数来构建小说内容的Markdown格式，包含标题、简介和章节内容。
def _build_export_markdown(bundle: dict) -> str:
    title = str(bundle.get("title", "")).strip() or f"小说_{bundle.get('novel_id', '')}"
    intro = str(bundle.get("intro", "") or "").strip()
    chapters = bundle.get("chapters", []) or []

    parts: list[str] = [f"# {title}", "", "## 文章简介", "", intro or "暂无简介", ""]
    for chapter in chapters:
        chapter_id = int(chapter.get("chapter_id", 0))
        chapter_title = str(chapter.get("chapter_title", "") or chapter.get("title", "")).strip()
        chapter_text = str(chapter.get("chapter_full_text", "") or "").strip()
        heading = f"第{chapter_id}章"
        if chapter_title:
            heading = f"{heading} {chapter_title}"
        parts.append(f"## {heading}")
        parts.append("")
        parts.append(chapter_text or "（本章暂无正文）")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"

# 定义一个函数生成唯一小说ID
def _generate_unique_novel_id(conn: sqlite3.Connection, max_attempts: int = 50) -> int:
    for _ in range(max_attempts):
        candidate = random.randint(100000, 999999)
        exists = conn.execute(
            "SELECT 1 FROM chapter_outlines WHERE novel_id = ?",
            (candidate,),
        ).fetchone()
        if exists is None:
            return candidate
    raise HTTPException(status_code=500, detail="Failed to generate unique novel id")

# 定义一个函数从数据库加载小说的完整数据
def _load_novel_bundle(conn: sqlite3.Connection, novel_id: int) -> dict:
    conn.row_factory = sqlite3.Row
    outline = conn.execute(
        """
        SELECT novel_id, writing_style, title, novel_intro, is_completed
        FROM chapter_outlines
        WHERE novel_id = ?
        """,
        (novel_id,),
    ).fetchone()
    if outline is None:
        raise HTTPException(status_code=404, detail="Novel not found")

    chapter_rows = conn.execute(
        """
        SELECT chapter_id, chapter_title, chapter_summary, chapter_full_text, word_count
        FROM chapter_summaries
        WHERE novel_id = ?
        ORDER BY chapter_id
        """,
        (novel_id,),
    ).fetchall()

    character_rows = conn.execute(
        """
        SELECT character_id, character_name, profile_detail
        FROM character_profiles
        WHERE novel_id = ?
        ORDER BY character_id
        """,
        (novel_id,),
    ).fetchall()

    # 获取章节的正文内容（用于拼接全文）
    full_text_parts: list[str] = []
    for row in chapter_rows:
        chapter_id = int(row["chapter_id"])
        chapter_title = str(row["chapter_title"] or "").strip()
        chapter_text = str(row["chapter_full_text"] or "").strip()
        if chapter_text:
            heading = f"第{chapter_id}章"
            if chapter_title:
                heading = f"{heading} {chapter_title}"
            full_text_parts.append(f"{heading}\n{chapter_text}")

    # 获取章节的正文内容（用于前端展示）
    chapters = [
        {
            "chapter_id": int(row["chapter_id"]),
            "chapter_title": str(row["chapter_title"] or ""),
            "title": str(row["chapter_title"] or ""),
            "chapter_summary": str(row["chapter_summary"] or ""),
            "chapter_full_text": str(row["chapter_full_text"] or ""),
            "word_count": int(row["word_count"] or 0),
        }
        for row in chapter_rows
    ]

    # 获取角色信息
    characters = [
        {
            "character_id": int(row["character_id"]),
            "character_name": str(row["character_name"] or ""),
            "profile_detail": str(row["profile_detail"] or ""),
        }
        for row in character_rows
    ]

    return {
        "novel_id": int(outline["novel_id"]),
        "title": str(outline["title"] or f"小说 #{int(outline['novel_id'])}"),
        "writing_style": str(outline["writing_style"] or ""),
        "intro": str(outline["novel_intro"] or ""),
        "is_completed": bool(int(outline["is_completed"] or 0)),
        "full_text": "\n\n".join(full_text_parts)
        or "当前暂无章节全文，请先进行创作或导入章节内容。",
        "chapters": chapters,
        "characters": characters,
    }

# 定义根路径的路由，返回首页HTML文件，并设置响应头以防止浏览器缓存。
@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers=_nocache_headers())

# 定义一个API路由，返回所有小说的基本信息列表。
@app.get("/api/novels")
def list_novels() -> dict:
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT novel_id, writing_style, title, novel_intro, is_completed
            FROM chapter_outlines
            ORDER BY novel_id
            """
        ).fetchall()
    finally:
        conn.close()

    novels = []
    for row in rows:
        novel_id = int(row["novel_id"])
        novels.append(
            {
                "novel_id": novel_id,
                "title": str(row["title"] or f"小说 #{novel_id}"),
                "writing_style": str(row["writing_style"] or ""),
                "chapter_intro": str(row["novel_intro"] or ""),
                "is_completed": bool(int(row["is_completed"] or 0)),
            }
        )

    return {"novels": novels}

# 定义一个API路由，生成一个新的唯一小说ID。
@app.get("/api/novels/new-id")
def new_novel_id() -> dict:
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        novel_id = _generate_unique_novel_id(conn)
    finally:
        conn.close()
    return {"novel_id": novel_id}

# 定义一个API路由，根据提供的写作风格和需求生成小说草稿，并返回生成结果。
@app.post("/api/agent/generate-draft")
def generate_novel_draft(payload: dict) -> dict:
    writing_style = str(payload.get("writing_style", "")).strip()
    requirements = str(payload.get("requirements", "")).strip()
    if not writing_style:
        raise HTTPException(status_code=400, detail="writing_style is required")
    if not requirements:
        raise HTTPException(status_code=400, detail="requirements is required")

    # 允许用户指定novel_id，如果没有提供或提供的ID已存在，则生成一个新的唯一ID
    novel_id_raw = payload.get("novel_id")
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        if novel_id_raw is None:
            novel_id = _generate_unique_novel_id(conn)
        else:
            novel_id = int(novel_id_raw)
            exists = conn.execute(
                "SELECT 1 FROM chapter_outlines WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
            if exists is not None:
                novel_id = _generate_unique_novel_id(conn)
    finally:
        conn.close()

    chain_input = {
        "novel_id": novel_id,
        "writing_style": writing_style,
        "requirements": requirements,
        # 额外信息字段，方便后续扩展使用
        "optional_context": str(payload.get("optional_context", "")).strip(),
    }
    try:
        # 直接调用链式智能体生成小说草稿，并返回结果
        return run_create_chain(chain_input)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"create chain failed: {exc}") from exc

# 定义一个API路由，保存小说的基本信息、角色信息和章节概要到数据库中。
@app.post("/api/novels")
def save_novel(payload: dict) -> dict:
    try:
        novel_id = int(payload.get("novel_id"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="valid novel_id is required") from exc

    writing_style = str(payload.get("writing_style", "")).strip()
    title = str(payload.get("title", "")).strip()
    intro = str(payload.get("intro", "")).strip()
    characters = payload.get("characters", [])
    chapters = payload.get("chapters", [])

    if not writing_style:
        raise HTTPException(status_code=400, detail="writing_style is required")
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    if not isinstance(characters, list) or not characters:
        raise HTTPException(status_code=400, detail="characters is required")
    if not isinstance(chapters, list) or not chapters:
        raise HTTPException(status_code=400, detail="chapters is required")

    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        # 事务开始，先检查novel_id是否已存在，如果存在则返回冲突错误；如果不存在则插入小说基本信息、角色信息和章节概要，最后提交事务。
        conn.execute("BEGIN")
        exists = conn.execute(
            "SELECT 1 FROM chapter_outlines WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        if exists is not None:
            raise HTTPException(status_code=409, detail="novel_id already exists")

        # 插入小说基本信息
        conn.execute(
            """
            INSERT INTO chapter_outlines (novel_id, is_completed, novel_intro, writing_style, title)
            VALUES (?, 0, ?, ?, ?)
            """,
            (novel_id, intro, writing_style, title),
        )

        # 插入角色信息
        for idx, char in enumerate(characters, start=1):
            character_id = _safe_int(char.get("character_id"), idx)
            character_name = str(char.get("character_name", "")).strip() or f"角色{idx}"
            profile_detail = str(char.get("profile_detail", "")).strip() or "待完善"
            conn.execute(
                """
                INSERT INTO character_profiles (novel_id, character_id, character_name, profile_detail)
                VALUES (?, ?, ?, ?)
                """,
                (novel_id, character_id, character_name, profile_detail),
            )

        # 插入章节概要
        for idx, chapter in enumerate(chapters, start=1):
            chapter_id = _safe_int(chapter.get("chapter_id"), idx)
            chapter_title = str(chapter.get("title", "")).strip()
            chapter_summary = str(chapter.get("summary", "")).strip()
            conn.execute(
                """
                INSERT INTO chapter_summaries (
                    novel_id, chapter_id, chapter_title, chapter_summary, chapter_full_text, word_count
                )
                VALUES (?, ?, ?, ?, '', 0)
                """,
                (
                    novel_id,
                    chapter_id,
                    chapter_title or f"第{chapter_id}章",
                    chapter_summary or f"第{chapter_id}章待完善",
                ),
            )

        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"failed to save novel: {exc}") from exc
    finally:
        conn.close()

    return {"ok": True, "novel_id": novel_id}

# 获取指定ID的小说详情
@app.get("/api/novels/{novel_id}")
def novel_detail(novel_id: int) -> dict:
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        return _load_novel_bundle(conn, novel_id)
    finally:
        conn.close()

# 定义一个API路由，将指定ID的小说内容导出为纯文本和Markdown格式，并保存到本地文件系统中。
@app.post("/api/novels/{novel_id}/export")
def export_novel_to_local(novel_id: int) -> dict:
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        bundle = _load_novel_bundle(conn, novel_id)
    finally:
        conn.close()

    output_dir = BASE_DIR / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_title = _safe_filename(bundle.get("title", ""))
    txt_name = f"{safe_title}.txt"
    md_name = f"{safe_title}.md"
    txt_path = output_dir / txt_name
    md_path = output_dir / md_name

    txt_path.write_text(_build_export_text(bundle), encoding="utf-8")
    md_path.write_text(_build_export_markdown(bundle), encoding="utf-8")

    return {
        "ok": True,
        "novel_id": novel_id,
        "title": bundle.get("title", ""),
        "output_file": str(txt_path),
        "output_files": {
            "txt": str(txt_path),
            "md": str(md_path),
        },
    }

# 定义一个API路由，更新指定ID的小说内容，包括基本信息、角色信息和章节概要。
# put一般是用于更新数据，而post一般是创建数据
@app.put("/api/novels/{novel_id}")
def update_novel_content(novel_id: int, payload: dict) -> dict:
    title = str(payload.get("title", "")).strip()
    writing_style = str(payload.get("writing_style", "")).strip()
    intro = str(payload.get("intro", "")).strip()
    characters = payload.get("characters", [])
    chapters = payload.get("chapters", [])

    if not title or not writing_style:
        raise HTTPException(status_code=400, detail="title and writing_style are required")
    if not isinstance(characters, list):
        raise HTTPException(status_code=400, detail="characters must be a list")
    if not isinstance(chapters, list):
        raise HTTPException(status_code=400, detail="chapters must be a list")

    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        conn.execute("BEGIN")
        exists = conn.execute(
            "SELECT 1 FROM chapter_outlines WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Novel not found")

        conn.execute(
            """
            UPDATE chapter_outlines
            SET title = ?, writing_style = ?, novel_intro = ?
            WHERE novel_id = ?
            """,
            (title, writing_style, intro, novel_id),
        )

        conn.execute("DELETE FROM character_profiles WHERE novel_id = ?", (novel_id,))
        for idx, char in enumerate(characters, start=1):
            character_id = _safe_int(char.get("character_id"), idx)
            name = str(char.get("character_name", "")).strip() or f"角色{idx}"
            profile = str(char.get("profile_detail", "")).strip() or "待完善"
            conn.execute(
                """
                INSERT INTO character_profiles (novel_id, character_id, character_name, profile_detail)
                VALUES (?, ?, ?, ?)
                """,
                (novel_id, character_id, name, profile),
            )

        conn.execute("DELETE FROM chapter_summaries WHERE novel_id = ?", (novel_id,))
        for idx, chapter in enumerate(chapters, start=1):
            chapter_id = _safe_int(chapter.get("chapter_id"), idx)
            chapter_title = str(chapter.get("chapter_title", chapter.get("title", ""))).strip() or f"第{chapter_id}章"
            chapter_summary = str(chapter.get("chapter_summary", "")).strip() or f"第{chapter_id}章待完善"
            full_text = str(chapter.get("chapter_full_text", "")).strip()
            word_count = _safe_int(chapter.get("word_count"), _word_count_cn(full_text))
            if word_count <= 0:
                word_count = _word_count_cn(full_text)
            conn.execute(
                """
                INSERT INTO chapter_summaries (
                    novel_id, chapter_id, chapter_title, chapter_summary, chapter_full_text, word_count
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (novel_id, chapter_id, chapter_title, chapter_summary, full_text, word_count),
            )

        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"update failed: {exc}") from exc
    finally:
        conn.close()

    return {"ok": True, "novel_id": novel_id}

# 定义一个API路由，用于续写指定ID的小说草稿
@app.post("/api/novels/{novel_id}/continue-draft")
def generate_continue_draft(novel_id: int, payload: dict) -> dict:
    user_requirement = str(payload.get("requirement", "")).strip()
    if not user_requirement:
        raise HTTPException(status_code=400, detail="requirement is required")

    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        novel = _load_novel_bundle(conn, novel_id)
    finally:
        conn.close()

    chain_input = {
        "novel_id": novel_id,
        "title": str(payload.get("title", novel.get("title", ""))),
        "writing_style": str(payload.get("writing_style", novel.get("writing_style", ""))),
        "intro": str(payload.get("intro", novel.get("intro", ""))),
        "user_requirement": user_requirement,
        "characters": payload.get("characters", novel.get("characters", [])),
        "chapters": novel.get("chapters", []),
    }
    try:
        result = run_continue_chain(chain_input)
    except Exception as exc:
        print("[continue-draft] chain_input:", {
            "novel_id": chain_input.get("novel_id"),
            "title": bool(str(chain_input.get("title", "")).strip()),
            "writing_style": bool(str(chain_input.get("writing_style", "")).strip()),
            "intro_len": len(str(chain_input.get("intro", "") or "")),
            "requirement_len": len(str(chain_input.get("user_requirement", "") or "")),
            "characters_count": len(chain_input.get("characters", []) or []),
            "chapters_count": len(chain_input.get("chapters", []) or []),
        })
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"continue chain failed: {exc}") from exc

    return {
        "novel_id": novel_id,
        "requirement": user_requirement,
        "retrieval": result.get("retrieval", {"summary_hits": [], "chunk_hits": []}),
        "chapter_draft": result.get("chapter_draft", {}),
        "review_notes": result.get("review_notes", []),
    }

# 定义一个API路由，保存指定ID的小说章节内容，包括章节概要、章节全文和字数统计。
@app.post("/api/novels/{novel_id}/chapters")
def save_new_chapter(novel_id: int, payload: dict) -> dict:
    chapter_id = _safe_int(payload.get("chapter_id"), 0)
    chapter_title = str(payload.get("chapter_title", payload.get("title", ""))).strip()
    chapter_summary = str(payload.get("chapter_summary", "")).strip()
    chapter_full_text = str(payload.get("chapter_full_text", "")).strip()

    if chapter_id <= 0:
        raise HTTPException(status_code=400, detail="valid chapter_id is required")
    if not chapter_title:
        chapter_title = f"第{chapter_id}章"
    if not chapter_summary:
        raise HTTPException(status_code=400, detail="chapter_summary is required")

    word_count = _safe_int(payload.get("word_count"), _word_count_cn(chapter_full_text))
    if word_count <= 0:
        word_count = _word_count_cn(chapter_full_text)

    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        conn.execute("BEGIN")
        exists_novel = conn.execute(
            "SELECT 1 FROM chapter_outlines WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        if exists_novel is None:
            raise HTTPException(status_code=404, detail="Novel not found")

        exists_chapter = conn.execute(
            """
            SELECT chapter_title, chapter_full_text
            FROM chapter_summaries
            WHERE novel_id = ? AND chapter_id = ?
            """,
            (novel_id, chapter_id),
        ).fetchone()
        if exists_chapter is not None:
            existing_title = str(exists_chapter[0] or "").strip()
            existing_full_text = str(exists_chapter[1] or "").strip()
            effective_title = chapter_title or existing_title or f"第{chapter_id}章"
            if existing_full_text:
                raise HTTPException(
                    status_code=409,
                    detail="chapter_id already has full text, please edit in write page before saving",
                )

            conn.execute(
                """
                UPDATE chapter_summaries
                SET chapter_title = ?, chapter_summary = ?, chapter_full_text = ?, word_count = ?
                WHERE novel_id = ? AND chapter_id = ?
                """,
                (effective_title, chapter_summary, chapter_full_text, word_count, novel_id, chapter_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO chapter_summaries (
                    novel_id, chapter_id, chapter_title, chapter_summary, chapter_full_text, word_count
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (novel_id, chapter_id, chapter_title, chapter_summary, chapter_full_text, word_count),
            )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"save chapter failed: {exc}") from exc
    finally:
        conn.close()

    return {"ok": True, "novel_id": novel_id, "chapter_id": chapter_id}

# 定义一个API路由，将指定ID的小说标记为已完成状态。
@app.post("/api/novels/{novel_id}/complete")
def complete_novel(novel_id: int) -> dict:
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.execute(
            """
            UPDATE chapter_outlines
            SET is_completed = 1
            WHERE novel_id = ?
            """,
            (novel_id,),
        )
        conn.commit()
    finally:
        conn.close()

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Novel not found")
    return {"ok": True, "novel_id": novel_id, "is_completed": True}


@app.get("/detail")
def detail_plain() -> FileResponse:
    return FileResponse(WEB_DIR / "detail.html", headers=_nocache_headers())


@app.get("/detail/{novel_id}")
def detail_with_id(novel_id: int) -> FileResponse:
    return FileResponse(WEB_DIR / "detail.html", headers=_nocache_headers())


@app.get("/create")
def create() -> FileResponse:
    return FileResponse(WEB_DIR / "create.html", headers=_nocache_headers())

# 无论是"/continue"还是"/continue/{novel_id}"，都会命中这个路由
# 与"/detail/{novel_id}"不同的是，一个在<a>，一个在<button>
@app.get("/continue")
def continue_write() -> FileResponse:
    return FileResponse(WEB_DIR / "write.html", headers=_nocache_headers())
