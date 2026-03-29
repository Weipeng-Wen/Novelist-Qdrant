from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from typing import Iterable
from dotenv import load_dotenv
from dataclasses import dataclass
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 定义数据类
@dataclass
class ChapterRow:
    novel_id: int
    chapter_id: int
    chapter_summary: str
    chapter_full_text: str
    word_count: int

# 在数据库中查询章节数据，并返回一个ChapterRow列表
def fetch_chapter_rows(sqlite_db_path: str) -> list[ChapterRow]:
    conn = sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT novel_id, chapter_id, chapter_summary, chapter_full_text, word_count
            FROM chapter_summaries
            ORDER BY novel_id, chapter_id
            """
        ).fetchall()
    finally:
        conn.close()

    return [
        ChapterRow(
            novel_id=int(r["novel_id"]),
            chapter_id=int(r["chapter_id"]),
            chapter_summary=str(r["chapter_summary"] or ""),
            chapter_full_text=str(r["chapter_full_text"] or ""),
            word_count=int(r["word_count"] or 0),
        )
        for r in rows
    ]

# 构建章节摘要的Document列表，每个Document包含章节摘要文本和相关元数据
def build_summary_documents(rows: Iterable[ChapterRow]) -> list[Document]:
    docs: list[Document] = []
    for row in rows:
        if not row.chapter_summary.strip():
            continue
        docs.append(
            Document(
                page_content=row.chapter_summary,
                metadata={
                    "novel_id": row.novel_id,
                    "chapter_id": row.chapter_id,
                    "doc_type": "chapter_summary",
                    "word_count": row.word_count,
                },
            )
        )
    return docs

# 构建章节全文的Document列表，使用文本分割器将章节全文拆分成多个块，每个块作为一个Document，并包含相关元数据
def build_fulltext_chunk_documents(rows: Iterable[ChapterRow], chunk_size: int = 900, overlap: int = 120) -> list[Document]:
    docs: list[Document] = []

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        separators=["\n\n", "\n", " ", ""]
    )
    
    for row in rows:
        chunks = splitter.split_text(row.chapter_full_text)
        for idx, chunk in enumerate(chunks):
            docs.append(
                Document(
                    page_content=chunk,
                    metadata={
                        "novel_id": row.novel_id,
                        "chapter_id": row.chapter_id,
                        "chunk_id": f"chunk_{idx}",
                        "doc_type": "chapter_full_text_chunk",
                    },
                )
            )
    return docs

def run() -> dict[str, list[Document]]:
    load_dotenv()

    project_root = Path(__file__).resolve().parents[1]
    sqlite_db_path = os.getenv("SQLITE_DB_PATH", str(project_root / "data" / "novel.db"))

    rows = fetch_chapter_rows(sqlite_db_path)

    chunk_size = int(os.getenv("RAG_CHUNK_SIZE", "900"))
    overlap = int(os.getenv("RAG_CHUNK_OVERLAP", "120"))

    docs_summary = build_summary_documents(rows=rows)
    docs_chunks = build_fulltext_chunk_documents(rows=rows, chunk_size=chunk_size, overlap=overlap)

    print("数据分块完成：")
    print(f"摘要文档数：{len(docs_summary)}")
    print(f"全文块文档数：{len(docs_chunks)}")

    return {"summary": docs_summary, "chunks": docs_chunks}