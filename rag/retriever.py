from typing import Iterable
from qdrant_client.http import models
from langchain_core.documents import Document
from langchain_community.retrievers import BM25Retriever
from langchain_qdrant import QdrantVectorStore


class Retriever:
    def __init__(
        self,
        vectorstore_summary: QdrantVectorStore,
        vectorstore_chunks: QdrantVectorStore,
        docs_summary: list[Document],
        docs_chunks: list[Document],
        novel_id: int,
    ):
        self.vectorstore_summary = vectorstore_summary
        self.vectorstore_chunks = vectorstore_chunks
        self.docs_summary = docs_summary
        self.docs_chunks = docs_chunks
        self.novel_id = novel_id

    # 稀疏检索（BM25）
    def _bm25_retrieve(self, documents: list[Document], query: str, k: int) -> list[Document]:
        if not documents:
            return []
        retriever = BM25Retriever.from_documents(documents=documents, k=k)
        return retriever.invoke(query)

    # 统一判断文档是否属于当前小说，兼容 metadata 中 novel_id 为 int/str 的情况
    def _is_same_novel(self, doc: Document) -> bool:
        value = doc.metadata.get("novel_id")
        if value is None:
            return False
        try:
            return int(value) == int(self.novel_id)
        except (TypeError, ValueError):
            return str(value) == str(self.novel_id)

    # 仅保留当前小说的文档
    def _filter_docs_by_novel(self, docs: list[Document]) -> list[Document]:
        return [doc for doc in docs if self._is_same_novel(doc)]

    # 密集检索（向量搜索）
    def _vector_retrieve(
        self,
        vectorstore: QdrantVectorStore,
        query: str,
        k: int,
        novel_id: int | None = None,
        chapter_ids: list[int] | None = None,
    ) -> list[Document]:
        search_kwargs: dict = {"k": k}
        
        # 构建过滤条件，支持 novel_id 和 chapter_id 的过滤
        must_conditions = []

        if novel_id is not None:
            must_conditions.append(
                models.FieldCondition(
                    key="metadata.novel_id",
                    match=models.MatchValue(value=novel_id),
                )
            )
        
        if chapter_ids:
            must_conditions.append(
                models.FieldCondition(
                    key="metadata.chapter_id",
                    match=models.MatchAny(any=chapter_ids),
                )
            )

        if must_conditions:
            search_kwargs["filter"] = models.Filter(must=must_conditions)
        
        retriever = vectorstore.as_retriever(search_kwargs=search_kwargs)
        return retriever.invoke(query)
    
    # 获取文本信息唯一标识符
    def _doc_key(self, doc: Document) -> str:
        novel_id = doc.metadata.get("novel_id", "")
        chapter_id = doc.metadata.get("chapter_id", "")
        chunk_id = doc.metadata.get("chunk_id", "summary_0")
        return f"{novel_id}:{chapter_id}:{chunk_id}"

    # RRF融合算法
    def _rrf_fuse(
        self,
        rank_lists: Iterable[list[Document]],
        top_k: int,
        rrf_k: int = 60,
    ) -> list[Document]:
        scores: dict[str, float] = {}
        docs_map: dict[str, Document] = {}

        for docs in rank_lists:
            for rank, doc in enumerate(docs, start=1):
                key = self._doc_key(doc)
                docs_map[key] = doc
                scores[key] = scores.get(key, 0.0) + (1.0 / (rrf_k + rank))

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return [docs_map[key] for key, _ in ranked[:top_k]]

    # 从摘要检索结果中提取章节ID列表
    def _extract_candidate_chapter_ids(self, summary_docs: list[Document]) -> list[int]:
        chapter_ids: list[int] = []
        for doc in summary_docs:
            value = doc.metadata.get("chapter_id")
            if value is None:
                continue
            try:
                chapter_id = int(value)
            except (TypeError, ValueError):
                continue
            if chapter_id not in chapter_ids:
                chapter_ids.append(chapter_id)
        return chapter_ids

    # 根据章节ID过滤文本块文档列表
    def _filter_chunk_docs_by_chapters(self, chapter_ids: list[int], docs_chunks: list[Document] | None = None) -> list[Document]:
        allowed = set(chapter_ids)
        source_docs = docs_chunks if docs_chunks is not None else self.docs_chunks
        return [
            doc
            for doc in source_docs
            if int(doc.metadata.get("chapter_id", -1)) in allowed
        ]

    # 主检索方法
    def retrieve(self, query: str, summary_top_k: int = 5, chunk_top_k: int = 10) -> dict[str, list[Document]]:
        # 先把候选文档限定到当前 novel_id，避免 BM25/RRF 造成跨小说串检索
        novel_summary_docs = self._filter_docs_by_novel(self.docs_summary)
        novel_chunk_docs = self._filter_docs_by_novel(self.docs_chunks)

        # 摘要检索
        summary_vector_docs = self._vector_retrieve(
            vectorstore=self.vectorstore_summary,
            query=query,
            k=max(summary_top_k * 3, 10),
            novel_id=self.novel_id,
        )
        summary_vector_docs = self._filter_docs_by_novel(summary_vector_docs)

        summary_bm25_docs = self._bm25_retrieve(
            documents=novel_summary_docs,
            query=query,
            k=max(summary_top_k * 3, 10),
        )
        summary_hits = self._rrf_fuse(
            rank_lists=[summary_vector_docs, summary_bm25_docs],
            top_k=summary_top_k,
        )
        summary_hits = self._filter_docs_by_novel(summary_hits)

        # 从摘要检索结果中提取候选章节ID，并过滤文本块文档列表
        chapter_ids = self._extract_candidate_chapter_ids(summary_hits)
        candidate_chunk_docs = self._filter_chunk_docs_by_chapters(chapter_ids, docs_chunks=novel_chunk_docs)

        # 文本块检索
        chunk_vector_docs = self._vector_retrieve(
            vectorstore=self.vectorstore_chunks,
            query=query,
            k=max(chunk_top_k * 3, 20),
            novel_id=self.novel_id,
            chapter_ids=chapter_ids,
        )
        chunk_vector_docs = self._filter_docs_by_novel(chunk_vector_docs)

        chunk_bm25_docs = self._bm25_retrieve(
            documents=candidate_chunk_docs,
            query=query,
            k=max(chunk_top_k * 3, 20),
        )
        chunk_hits = self._rrf_fuse(
            rank_lists=[chunk_vector_docs, chunk_bm25_docs],
            top_k=chunk_top_k,
        )
        chunk_hits = self._filter_docs_by_novel(chunk_hits)

        return {
            "summary_hits": summary_hits,
            "chunk_hits": chunk_hits,
        }
