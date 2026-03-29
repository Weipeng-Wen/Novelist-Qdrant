from typing import TypedDict, Any, Literal
import json
import re
import ast

from langgraph.graph import StateGraph, START, END

from .llm import get_chat_model
from .prompt import CREATE_GENERATOR_SYSTEM_PROMPT, CREATE_REVIEWER_SYSTEM_PROMPT, QUERY_ANALYSIS_SYSTEM_PROMPT, CONTINUE_WRITER_SYSTEM_PROMPT, CONTINUE_REVIEWER_SYSTEM_PROMPT


# 定义创建状态
class CreateState(TypedDict):
    novel_id: int
    novel_title: str
    novel_intro: str
    writing_style: str
    requirements: str
    characters: list[dict[str, Any]]
    chapters: list[dict[str, Any]]
    suggestions: str
    iteration: int
    review_status: str

# 定义续写状态
class ContinueState(TypedDict):
    novel_id: int
    novel_title: str
    novel_intro: str
    writing_style: str
    requirements: str
    characters: list[dict[str, Any]]
    chapters: dict[str, Any]
    chapter_draft: dict[str, Any]
    review_notes: list[str]
    suggestions: str
    iteration: int
    review_status: str
    rag_retrieval: dict[str, list[dict[str, Any]]]
    analysis_query: str
    next_chapter_id: int


# 最大迭代次数，超过后即使未通过审查也强制结束，返回最终结果和最后一次审查意见。
MAX_ITERATIONS = 3

# 创作链
class CreateChain:
    def __init__(self):
        self.chat_model = get_chat_model()
        self.graph = None
        self.build()

    # 安全解析 LLM 输出的 JSON，兼容直接 JSON、Markdown 包裹的 JSON、以及文本中嵌入的 JSON。
    def _safe_parse_json(self, text: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        default = default or {}
        raw = (text or "").strip()
        if not raw:
            return default

        def _normalize(parsed: Any) -> dict[str, Any]:
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        return item
            return default

        try:
            parsed = json.loads(raw)
            normalized = _normalize(parsed)
            if normalized:
                return normalized
        except Exception:
            pass

        fenced = re.search(r"```(?:json)?\s*([\[{].*[\]}])\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            try:
                parsed = json.loads(fenced.group(1))
                normalized = _normalize(parsed)
                if normalized:
                    return normalized
            except Exception:
                pass

        left = raw.find("{")
        right = raw.rfind("}")
        if left != -1 and right != -1 and right > left:
            try:
                parsed = json.loads(raw[left : right + 1])
                normalized = _normalize(parsed)
                if normalized:
                    return normalized
            except Exception:
                pass

        list_left = raw.find("[")
        list_right = raw.rfind("]")
        if list_left != -1 and list_right != -1 and list_right > list_left:
            try:
                parsed = json.loads(raw[list_left : list_right + 1])
                normalized = _normalize(parsed)
                if normalized:
                    return normalized
            except Exception:
                pass

        try:
            parsed = ast.literal_eval(raw)
            normalized = _normalize(parsed)
            if normalized:
                return normalized
        except Exception:
            pass

        return default

    # 调用 LLM 完成生成或审查任务，并安全解析返回的 JSON 结果。
    def _invoke_json(self, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "请基于以下输入完成任务，并且只输出一个合法 JSON 对象：\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ]
        response = self.chat_model.invoke(messages)
        content = response.content if hasattr(response, "content") else ""

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            content_text = "".join(parts)
        else:
            content_text = str(content)

        parsed = self._safe_parse_json(content_text, default={})
        if parsed:
            return parsed

        retry_messages = [
            {"role": "system", "content": system_prompt + "\n你必须只输出一个可被 json.loads 解析的 JSON 对象。"},
            {
                "role": "user",
                "content": (
                    "你上一次没有返回可解析JSON。请立刻重试，只输出JSON对象，不要解释文本：\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ]
        retry_resp = self.chat_model.invoke(retry_messages)
        retry_content = retry_resp.content if hasattr(retry_resp, "content") else ""
        retry_text = str(retry_content)
        retry_parsed = self._safe_parse_json(retry_text, default={})
        if retry_parsed:
            return retry_parsed

        preview = content_text[:300].replace("\n", "\\n")
        retry_preview = retry_text[:300].replace("\n", "\\n")
        print(f"[continue-json-parse-failed] preview={preview}")
        print(f"[continue-json-parse-failed] retry_preview={retry_preview}")
        return {}

    # 创造智能体
    def create_draft(self, state: CreateState) -> dict[str, Any]:
        if state["iteration"] > 0:
            input_data = {
                "novel_id": state["novel_id"],
                "novel_title": state["novel_title"],
                "novel_intro": state["novel_intro"],
                "writing_style": state["writing_style"],
                "requirements": state["requirements"],
                "characters": state["characters"],
                "chapters": state["chapters"],
                "suggestions": state["suggestions"],
            }
        else:
            input_data = {
                "novel_id": state["novel_id"],
                "writing_style": state["writing_style"],
                "requirements": state["requirements"],
            }

        result = self._invoke_json(CREATE_GENERATOR_SYSTEM_PROMPT, payload=input_data)

        raw_chapters = result.get("chapters", state["chapters"])
        normalized_chapters: list[dict[str, Any]] = []
        if isinstance(raw_chapters, list):
            for idx, item in enumerate(raw_chapters, start=1):
                if not isinstance(item, dict):
                    continue
                chapter_id = item.get("chapter_id", idx)
                chapter_title = str(item.get("title", item.get("chapter_title", "")) or "")
                chapter_summary = str(item.get("summary", item.get("chapter_summary", "")) or "")
                normalized_chapters.append(
                    {
                        **item,
                        "chapter_id": chapter_id,
                        "title": chapter_title,
                        "chapter_title": chapter_title,
                        "summary": chapter_summary,
                        "chapter_summary": chapter_summary,
                    }
                )

        # 不用返回整个state，因为他会根据你返回的字段自动覆盖之前的state
        # 没必要每次都返回全部字段，保持增量更新即可。
        return {
            "novel_title": str(result.get("novel_title", state["novel_title"]) or state["novel_title"]),
            "novel_intro": str(result.get("novel_intro", state["novel_intro"]) or state["novel_intro"]),
            "characters": result.get("characters", state["characters"]) if isinstance(result.get("characters"), list) else state["characters"],
            "chapters": normalized_chapters if normalized_chapters else state["chapters"],
        }

    # 审查智能体
    def review_draft(self, state: CreateState) -> dict[str, Any]:
        input_data = {
            "novel_id": state["novel_id"],
            "novel_title": state["novel_title"],
            "novel_intro": state["novel_intro"],
            "writing_style": state["writing_style"],
            "requirements": state["requirements"],
            "characters": state["characters"],
            "chapters": state["chapters"],
            "suggestions": state["suggestions"],
        }

        result = self._invoke_json(CREATE_REVIEWER_SYSTEM_PROMPT, payload=input_data)

        temp = str(result.get("review_status", "FINISH")).upper()
        next_suggestions = str(result.get("suggestions", state["suggestions"]) or state["suggestions"])

        if state["iteration"] < MAX_ITERATIONS and temp != "FINISH":
            return {
                "review_status": "REJECT",
                "suggestions": next_suggestions,
                "iteration": state["iteration"] + 1,
            }

        return {
            "review_status": "FINISH",
            "suggestions": next_suggestions if temp != "FINISH" else "",
        }

    # 分支路由
    def route_after_review(self, state: CreateState) -> Literal["reject", "finish"]:
        status = str(state.get("review_status", "FINISH")).upper()
        return "reject" if status != "FINISH" else "finish"

    # 最终结果整理输出
    def finish(self, state: CreateState) -> dict[str, Any]:
        return {
            "novel_id": state["novel_id"],
            "title": state["novel_title"],
            "intro": state["novel_intro"],
            "writing_style": state["writing_style"],
            "requirements": state["requirements"],
            "characters": state["characters"],
            "chapters": state["chapters"],
        }

    def build(self) -> None:
        graph = StateGraph(CreateState)

        graph.add_node("create_draft", self.create_draft)
        graph.add_node("review_draft", self.review_draft)
        graph.add_node("finish", self.finish)

        graph.add_edge(START, "create_draft")
        graph.add_edge("create_draft", "review_draft")
        graph.add_conditional_edges(
            "review_draft",
            self.route_after_review,
            {
                "reject": "create_draft",
                "finish": "finish",
            },
        )
        graph.add_edge("finish", END)

        self.graph = graph.compile()

    def run(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        state: CreateState = {
            "novel_id": int(initial_state.get("novel_id", 0) or 0),
            "novel_title": "",
            "novel_intro": "",
            "writing_style": str(initial_state.get("writing_style", "") or ""),
            "requirements": str(initial_state.get("requirements", initial_state.get("requirement", "")) or ""),
            "characters": [],
            "chapters": [],
            "suggestions": "",
            "iteration": 0,
            "review_status": "",
        }
        output = self.graph.invoke(state)

        # 统一 create API 返回结构，兼容前端字段读取（title/intro/chapter.title/summary）
        chapters = output.get("chapters", []) if isinstance(output, dict) else []
        normalized_chapters: list[dict[str, Any]] = []
        if isinstance(chapters, list):
            for idx, item in enumerate(chapters, start=1):
                if not isinstance(item, dict):
                    continue
                chapter_id = item.get("chapter_id", idx)
                chapter_title = str(item.get("title", item.get("chapter_title", "")) or "")
                chapter_summary = str(item.get("summary", item.get("chapter_summary", "")) or "")
                normalized_chapters.append(
                    {
                        **item,
                        "chapter_id": chapter_id,
                        "title": chapter_title,
                        "chapter_title": chapter_title,
                        "summary": chapter_summary,
                        "chapter_summary": chapter_summary,
                    }
                )

        if not isinstance(output, dict):
            output = {}

        return {
            "novel_id": int(output.get("novel_id", state["novel_id"]) or state["novel_id"]),
            "title": str(output.get("title", output.get("novel_title", state["novel_title"])) or ""),
            "intro": str(output.get("intro", output.get("novel_intro", state["novel_intro"])) or ""),
            "writing_style": str(output.get("writing_style", state["writing_style"]) or state["writing_style"]),
            "requirements": str(output.get("requirements", state["requirements"]) or state["requirements"]),
            "characters": output.get("characters", state["characters"]) if isinstance(output.get("characters", state["characters"]), list) else state["characters"],
            "chapters": normalized_chapters,
        }
    
# 续写链
class ContinueChain:
    def __init__(self):
        self.chat_model = get_chat_model()
        self.graph = None
        self.build()

    # 安全解析 LLM 输出的 JSON，兼容直接 JSON、Markdown 包裹的 JSON、文本中嵌入 JSON、及 Python 风格字典。
    def _safe_parse_json(self, text: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        default = default or {}
        raw = (text or "").strip()
        if not raw:
            return default

        def _normalize(parsed: Any) -> dict[str, Any]:
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        return item
            return default

        try:
            parsed = json.loads(raw)
            normalized = _normalize(parsed)
            if normalized:
                return normalized
        except Exception:
            pass

        fenced = re.search(r"```(?:json)?\s*([\[{].*[\]}])\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            try:
                parsed = json.loads(fenced.group(1))
                normalized = _normalize(parsed)
                if normalized:
                    return normalized
            except Exception:
                pass

        left = raw.find("{")
        right = raw.rfind("}")
        if left != -1 and right != -1 and right > left:
            try:
                parsed = json.loads(raw[left : right + 1])
                normalized = _normalize(parsed)
                if normalized:
                    return normalized
            except Exception:
                pass

        list_left = raw.find("[")
        list_right = raw.rfind("]")
        if list_left != -1 and list_right != -1 and list_right > list_left:
            try:
                parsed = json.loads(raw[list_left : list_right + 1])
                normalized = _normalize(parsed)
                if normalized:
                    return normalized
            except Exception:
                pass

        try:
            parsed = ast.literal_eval(raw)
            normalized = _normalize(parsed)
            if normalized:
                return normalized
        except Exception:
            pass

        return default

    # 调用 LLM 完成生成或审查任务，并安全解析返回的 JSON 结果。
    def _invoke_json(self, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "请基于以下输入完成任务，并且只输出一个合法 JSON 对象：\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ]
        response = self.chat_model.invoke(messages)
        content = response.content if hasattr(response, "content") else ""

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            content_text = "".join(parts)
        else:
            content_text = str(content)

        parsed = self._safe_parse_json(content_text, default={})
        if parsed:
            return parsed

        retry_messages = [
            {"role": "system", "content": system_prompt + "\n你必须只输出一个可被 json.loads 解析的 JSON 对象。"},
            {
                "role": "user",
                "content": (
                    "你上一次没有返回可解析JSON。请立刻重试，只输出JSON对象，不要解释文本：\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ]
        retry_resp = self.chat_model.invoke(retry_messages)
        retry_content = retry_resp.content if hasattr(retry_resp, "content") else ""
        retry_text = str(retry_content)
        retry_parsed = self._safe_parse_json(retry_text, default={})
        if retry_parsed:
            return retry_parsed

        preview = content_text[:300].replace("\n", "\\n")
        retry_preview = retry_text[:300].replace("\n", "\\n")
        print(f"[continue-json-parse-failed] preview={preview}")
        print(f"[continue-json-parse-failed] retry_preview={retry_preview}")
        return {}

    # 计算下一章章节ID，确保连续递增且不重复
    def _compute_next_chapter_id(self, chapters: list[dict[str, Any]]) -> int:
        if not chapters:
            return 1
        chapter_ids: list[int] = []
        for idx, chapter in enumerate(chapters, start=1):
            if not isinstance(chapter, dict):
                continue
            raw_id = chapter.get("chapter_id", idx)
            try:
                chapter_ids.append(int(raw_id))
            except Exception:
                continue
        return (max(chapter_ids) + 1) if chapter_ids else (len(chapters) + 1)
    
    # 优先续写“第一个正文为空”的章节；若不存在，则创建下一章占位信息。
    def _pick_target_chapter(self, chapters: list[dict[str, Any]], next_chapter_id: int) -> dict[str, Any]:
        
        for idx, chapter in enumerate(chapters, start=1):
            if not isinstance(chapter, dict):
                continue
            full_text = str(chapter.get("chapter_full_text", "") or "").strip()
            if full_text:
                continue
            chapter_id_raw = chapter.get("chapter_id", idx)
            try:
                chapter_id = int(chapter_id_raw)
            except Exception:
                chapter_id = idx
            title = str(chapter.get("chapter_title", chapter.get("title", "")) or f"第{chapter_id}章")
            summary = str(chapter.get("chapter_summary", chapter.get("summary", "")) or "")
            return {
                "chapter_id": chapter_id,
                "chapter_title": title,
                "title": title,
                "chapter_summary": summary,
                "summary": summary,
                "chapter_full_text": "",
                "word_count": 0,
            }

        return {
            "chapter_id": next_chapter_id,
            "chapter_title": f"第{next_chapter_id}章",
            "title": f"第{next_chapter_id}章",
            "chapter_summary": "",
            "summary": "",
            "chapter_full_text": "",
            "word_count": 0,
        }

    # 计算文本字数
    def _word_count(self, text: str) -> int:
        return len(re.sub(r"\s+", "", text or ""))

    # 从模型输出中提取章节草稿，兼容不同字段名和结构。
    def _extract_chapter_draft(self, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}

        for key in ("chapters", "chapter_draft", "chapter", "draft", "data", "result"):
            value = result.get(key)
            if isinstance(value, list):
                value = value[0] if value else {}
            if isinstance(value, dict):
                nested = value.get("chapters")
                if isinstance(nested, dict):
                    return nested
                return value

        if any(
            k in result
            for k in (
                "chapter_full_text",
                "chapter_title",
                "chapter_summary",
                "full_text",
                "content",
                "text",
                "body",
                "word_count",
            )
        ):
            return result
        return {}

    # 兼容不同模型输出字段名，尽可能提取正文文本。
    def _extract_full_text(self, raw_draft: dict[str, Any]) -> str:
        if not isinstance(raw_draft, dict):
            return ""

        candidates = [
            raw_draft.get("chapter_full_text"),
            raw_draft.get("full_text"),
            raw_draft.get("content"),
            raw_draft.get("text"),
            raw_draft.get("body"),
        ]
        for value in candidates:
            text = str(value or "")
            if text.strip():
                return text

        paragraphs = raw_draft.get("paragraphs")
        if isinstance(paragraphs, list):
            merged = "\n".join(str(item or "").strip() for item in paragraphs if str(item or "").strip())
            if merged.strip():
                return merged

        return ""

    # 当JSON结构不稳定时，兜底直接生成正文，避免整条链路报500。
    def _invoke_plain_text_chapter(self, payload: dict[str, Any]) -> str:
        chapter = payload.get("chapters", {}) if isinstance(payload.get("chapters", {}), dict) else {}
        prompt_messages = [
            {
                "role": "system",
                "content": (
                    "你是小说续写助手。只输出章节正文，不要JSON，不要章节标题行。"
                    "正文至少6段，段首使用两个全角空格“　　”。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "novel_title": payload.get("novel_title", ""),
                        "writing_style": payload.get("writing_style", ""),
                        "requirements": payload.get("requirements", ""),
                        "chapter_id": payload.get("next_chapter_id", 0),
                        "chapter_title": chapter.get("chapter_title", chapter.get("title", "")),
                        "chapter_summary": chapter.get("chapter_summary", chapter.get("summary", "")),
                        "rag_retrieval": payload.get("rag_retrieval", {"summary_hits": [], "chunk_hits": []}),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        resp = self.chat_model.invoke(prompt_messages)
        content = resp.content if hasattr(resp, "content") else ""
        text = str(content or "").strip()
        if not text:
            return ""
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        normalized_parts = [p if p.startswith("　　") else f"　　{p}" for p in parts]
        return "\n".join(normalized_parts).strip()

    # 将检索结果文档对象序列化为前端可用的字典格式
    def _serialize_retrieval_doc(self, doc: Any, kind: Literal["summary", "chunk"]) -> dict[str, Any]:
        metadata = getattr(doc, "metadata", {}) or {}
        try:
            chapter_id = int(metadata.get("chapter_id", 0) or 0)
        except Exception:
            chapter_id = 0

        content = str(getattr(doc, "page_content", "") or "")
        if kind == "summary":
            return {
                "chapter_id": chapter_id,
                "summary": content[:220],
            }
        return {
            "chapter_id": chapter_id,
            "text": content[:220],
        }

    # 解析智能体
    def analize_query(self, state: ContinueState) -> dict[str, Any]:
        input_data = {
            "novel_id": state["novel_id"],
            "novel_title": state["novel_title"],
            "novel_intro": state["novel_intro"],
            "writing_style": state["writing_style"],
            "requirements": state["requirements"],
            "characters": state["characters"],
            "chapters": state["chapters"],
        }
        
        result = self._invoke_json(QUERY_ANALYSIS_SYSTEM_PROMPT, payload=input_data)

        fallback_query = (
            state["requirements"]
            or f"{state['novel_title']} {state['writing_style']} 续写"
        )
        return {
            "analysis_query": str(result.get("analysis_query", "") or fallback_query),
        }
    
    # RAG检索
    def retrieve(self, state: ContinueState) -> dict[str, Any]:
        query = str(state.get("analysis_query", "") or "").strip()
        if not query:
            query = state["requirements"] or "小说续写"

        try:
            from rag.document_split import run
            from rag.vector_store import VectorStore
            from rag.retriever import Retriever

            docs = run()
            docs_summary = docs.get("summary", [])
            docs_chunks = docs.get("chunks", [])

            if not docs_summary and not docs_chunks:
                return {"rag_retrieval": {"summary_hits": [], "chunk_hits": []}}

            vector_builder = VectorStore(summary_docs=docs_summary, chunks_docs=docs_chunks)
            vectorstore_summary, vectorstore_chunks = vector_builder.run()
            if vectorstore_summary is None or vectorstore_chunks is None:
                return {"rag_retrieval": {"summary_hits": [], "chunk_hits": []}}

            retriever = Retriever(
                vectorstore_summary=vectorstore_summary,
                vectorstore_chunks=vectorstore_chunks,
                docs_summary=docs_summary,
                docs_chunks=docs_chunks,
                novel_id=state["novel_id"],
            )
            results = retriever.retrieve(query=query)
            summary_hits = [
                self._serialize_retrieval_doc(doc, "summary")
                for doc in results.get("summary_hits", [])
            ]
            chunk_hits = [
                self._serialize_retrieval_doc(doc, "chunk")
                for doc in results.get("chunk_hits", [])
            ]
            return {"rag_retrieval": {"summary_hits": summary_hits, "chunk_hits": chunk_hits}}
        except Exception:
            # RAG失败时降级为空检索，不阻断续写链执行
            return {"rag_retrieval": {"summary_hits": [], "chunk_hits": []}}

    # 续写智能体
    def create_content(self, state: ContinueState) -> dict[str, Any]:
        if state["iteration"] > 0:
            input_data = {
                "novel_id": state["novel_id"],
                "novel_title": state["novel_title"],
                "novel_intro": state["novel_intro"],
                "writing_style": state["writing_style"],
                "requirements": state["requirements"],
                "characters": state["characters"],
                "chapters": state["chapters"],
                "next_chapter_id": state["next_chapter_id"],
                "rag_retrieval": state["rag_retrieval"],
                "suggestions": state["suggestions"],
            }
        else:
            input_data = {
                "novel_id": state["novel_id"],
                "novel_title": state["novel_title"],
                "novel_intro": state["novel_intro"],
                "writing_style": state["writing_style"],
                "requirements": state["requirements"],
                "characters": state["characters"],
                "chapters": state["chapters"],
                "next_chapter_id": state["next_chapter_id"],
                "rag_retrieval": state["rag_retrieval"],
            }
        
        result = self._invoke_json(CONTINUE_WRITER_SYSTEM_PROMPT, payload=input_data)
        raw_draft = self._extract_chapter_draft(result)
        if not raw_draft:
            fallback_text = self._invoke_plain_text_chapter(input_data)
            chapter_info = state.get("chapters", {}) if isinstance(state.get("chapters", {}), dict) else {}
            if fallback_text:
                raw_draft = {
                    "chapter_id": state["next_chapter_id"],
                    "chapter_title": str(chapter_info.get("chapter_title", chapter_info.get("title", f"第{state['next_chapter_id']}章"))),
                    "chapter_summary": str(chapter_info.get("chapter_summary", chapter_info.get("summary", ""))),
                    "chapter_full_text": fallback_text,
                    "word_count": self._word_count(fallback_text),
                }
            else:
                raise ValueError("continue writer output is not a JSON object")

        chapter_id = raw_draft.get("chapter_id", state["next_chapter_id"])
        try:
            chapter_id = int(chapter_id)
        except Exception:
            chapter_id = state["next_chapter_id"]

        title = str(raw_draft.get("title", raw_draft.get("chapter_title", "")) or "")
        summary = str(raw_draft.get("chapter_summary", raw_draft.get("summary", "")) or "")
        full_text = self._extract_full_text(raw_draft)
        if not full_text.strip():
            # 当首轮输出缺失正文时，自动重试一次，降低偶发性空结果导致的 500。
            retry_payload = {**input_data, "retry_reason": "上次输出缺失正文，请仅返回合法JSON并确保chapter_full_text非空。"}
            retry_result = self._invoke_json(CONTINUE_WRITER_SYSTEM_PROMPT, payload=retry_payload)
            retry_raw_draft = self._extract_chapter_draft(retry_result)
            retry_full_text = self._extract_full_text(retry_raw_draft)
            if retry_full_text.strip():
                result = retry_result
                raw_draft = retry_raw_draft
                full_text = retry_full_text
            else:
                keys = sorted(list(raw_draft.keys())) if isinstance(raw_draft, dict) else []
                retry_keys = sorted(list(retry_raw_draft.keys())) if isinstance(retry_raw_draft, dict) else []
                raise ValueError(
                    "continue writer returned empty chapter_full_text "
                    f"(keys={keys}, retry_keys={retry_keys})"
                )
        word_count = raw_draft.get("word_count", self._word_count(full_text))
        try:
            word_count = int(word_count)
        except Exception:
            word_count = self._word_count(full_text)

        normalized_draft = {
            "chapter_id": chapter_id,
            "title": title or f"第{chapter_id}章",
            "chapter_title": title or f"第{chapter_id}章",
            "chapter_summary": summary,
            "summary": summary,
            "chapter_full_text": full_text,
            "word_count": max(word_count, 0),
        }
        return {
            "chapter_draft": normalized_draft,
        }

    # 审查智能体
    def review_content(self, state: ContinueState) -> dict[str, Any]:
        input_data = {
            "novel_id": state["novel_id"],
            "novel_title": state["novel_title"],
            "novel_intro": state["novel_intro"],
            "writing_style": state["writing_style"],
            "requirements": state["requirements"],
            "characters": state["characters"],
            "chapters": state["chapters"],
            "chapter_draft": state["chapter_draft"],
            "rag_retrieval": state["rag_retrieval"],
            "suggestions": state["suggestions"],
        }
        result = self._invoke_json(CONTINUE_REVIEWER_SYSTEM_PROMPT, payload=input_data)
        
        temp = str(result.get("review_status", "REJECT")).upper()
        next_suggestions = str(result.get("suggestions", state["suggestions"]) or state["suggestions"])
        if state["iteration"] < MAX_ITERATIONS and temp != "FINISH":
            return {
                "review_status": "REJECT",
                "suggestions": next_suggestions,
                "iteration": state["iteration"] + 1,
                "review_notes": state["review_notes"] + [next_suggestions] if next_suggestions else state["review_notes"],
            }
        review_note = "审阅通过，章节草稿可保存。"
        if temp != "FINISH" and next_suggestions:
            review_note = f"达到最大迭代次数，返回最后建议：{next_suggestions}"
        return {
            "review_status": "FINISH",
            "suggestions": next_suggestions if temp != "FINISH" else "",
            "review_notes": state["review_notes"] + [review_note],
        }

    # 分支路由
    def route_after_review(self, state: ContinueState) -> Literal["reject", "finish"]:
        status = str(state.get("review_status", "FINISH")).upper()
        return "reject" if status != "FINISH" else "finish"

    def build(self) -> None:
        graph = StateGraph(ContinueState)

        graph.add_node("analize_query", self.analize_query)
        graph.add_node("retrieve", self.retrieve)
        graph.add_node("create_content", self.create_content)
        graph.add_node("review_content", self.review_content)
        graph.add_node("finish", lambda state: {})

        graph.add_edge(START, "analize_query")
        graph.add_edge("analize_query", "retrieve")
        graph.add_edge("retrieve", "create_content")
        graph.add_edge("create_content", "review_content")
        graph.add_conditional_edges(
            "review_content",
            self.route_after_review,
            {
                "reject": "create_content",
                "finish": "finish",
            },
        )
        graph.add_edge("finish", END)

        self.graph = graph.compile()

    def run(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        chapters = initial_state.get("chapters", [])
        if not isinstance(chapters, list):
            chapters = []
        next_chapter_id = self._compute_next_chapter_id(chapters)
        target_chapter = self._pick_target_chapter(chapters, next_chapter_id)

        state: ContinueState = {
            "novel_id": int(initial_state.get("novel_id", 0) or 0),
            "novel_title": str(initial_state.get("novel_title", initial_state.get("title", "")) or ""),
            "novel_intro": str(initial_state.get("novel_intro", initial_state.get("intro", "")) or ""),
            "writing_style": str(initial_state.get("writing_style", "") or ""),
            "requirements": str(
                initial_state.get(
                    "requirements",
                    initial_state.get("user_requirement", initial_state.get("requirement", "")),
                )
                or ""
            ),
            "characters": initial_state.get("characters", []) if isinstance(initial_state.get("characters", []), list) else [],
            "chapters": target_chapter,
            "chapter_draft": {},
            "review_notes": [],
            "suggestions": "",
            "iteration": 0,
            "review_status": "",
            "rag_retrieval": {"summary_hits": [], "chunk_hits": []},
            "analysis_query": "",
            "next_chapter_id": int(target_chapter.get("chapter_id", next_chapter_id) or next_chapter_id),
        }

        output = self.graph.invoke(state)
        if not isinstance(output, dict):
            output = {}
        chapter_draft = output.get("chapter_draft", {})
        if not isinstance(chapter_draft, dict):
            chapter_draft = {}
        retrieval = output.get("rag_retrieval", {"summary_hits": [], "chunk_hits": []})
        if not isinstance(retrieval, dict):
            retrieval = {"summary_hits": [], "chunk_hits": []}
        review_notes = output.get("review_notes", [])
        if not isinstance(review_notes, list):
            review_notes = []

        return {
            "retrieval": retrieval,
            "chapter_draft": {
                "chapter_id": int(chapter_draft.get("chapter_id", state["next_chapter_id"]) or state["next_chapter_id"]),
                "title": str(chapter_draft.get("title", chapter_draft.get("chapter_title", f"第{state['next_chapter_id']}章"))),
                "chapter_summary": str(chapter_draft.get("chapter_summary", chapter_draft.get("summary", "")) or ""),
                "chapter_full_text": str(chapter_draft.get("chapter_full_text", "") or ""),
                "word_count": int(chapter_draft.get("word_count", 0) or 0),
            },
            "review_notes": review_notes,
        }

# 对外接口函数，供路由调用
def run_create_chain(initial_state: dict[str, Any]) -> dict[str, Any]:
    chain = CreateChain()
    return chain.run(initial_state)

# 对外接口函数，供路由调用
def run_continue_chain(initial_state: dict[str, Any]) -> dict[str, Any]:
    chain = ContinueChain()
    return chain.run(initial_state)
