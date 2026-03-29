from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv
from langchain_core.documents import Document
from qdrant_client import QdrantClient
from langchain_qdrant import QdrantVectorStore
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client.http.models import Distance, VectorParams,PayloadSchemaType


class VectorStore:
    def __init__(self, summary_docs: list[Document], chunks_docs: list[Document]):
        self.summary_docs = summary_docs
        self.chunks_docs = chunks_docs
        self.vectorstore_summary = None
        self.vectorstore_chunks = None

    # 将Document列表中的文档添加到Qdrant向量数据库中，如果文档已经存在则更新，最后持久化数据库
    def embed_and_store_documents(self, docs: list[Document], persist_directory: str, embedding_model: str) -> QdrantVectorStore:
        if not docs:
            return

        # 创建嵌入模型
        embeddings = HuggingFaceEmbeddings(model_name=embedding_model)

        # 用于连接Qdrant云服务的客户端配置，替换为你自己的URL和API Key
        qdrant_client = QdrantClient(
            url="https://7a560453-ea3c-4eb5-8969-bbcdafe5cf05.us-east4-0.gcp.cloud.qdrant.io:6333", 
            api_key="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIn0.w--uUIi6PWWJbwfnHsMWIgD--bdA6HnUqdIABreY1z0",
        )

        # 为每个集合创建有效载荷索引，以支持基于 novel_id 和 chapter_id 的过滤查询
        for collection in ["summary", "chunks"]:
            qdrant_client.create_payload_index(collection, "metadata.novel_id", PayloadSchemaType.INTEGER)
            qdrant_client.create_payload_index(collection, "metadata.chapter_id", PayloadSchemaType.INTEGER)
            print(f"Indexes created for {collection}")

        # 计算嵌入维度大小，通常是模型输出的向量维度
        dim_size = len(embeddings.embed_query("test"))

        # 如果集合不存在，则创建集合
        if not qdrant_client.collection_exists(collection_name=persist_directory):
            qdrant_client.create_collection(
                collection_name=persist_directory,
                vectors_config=VectorParams(size=dim_size, distance=Distance.COSINE),
            )
    
        # 创建或更新Qdrant向量数据库，并持久化
        vectorstore = QdrantVectorStore(
            client=qdrant_client,
            collection_name=persist_directory,
            embedding=embeddings,
        )

        # 将文档添加到向量数据库中，如果文档已经存在则更新
        vectorstore.add_documents(docs)

        return vectorstore

    def run(self):
        load_dotenv()

        embedding_model = os.getenv("HUGGINGFACEEMBEDDINGS_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    
        sumumary_persist_dir = "summary"
        chunks_persist_dir = "chunks"

        # 向量数据库中插入或更新文档，并持久化
        self.vectorstore_summary = self.embed_and_store_documents(
            docs=self.summary_docs,
            persist_directory=sumumary_persist_dir,
            embedding_model=embedding_model,
        )
        self.vectorstore_chunks = self.embed_and_store_documents(
            docs=self.chunks_docs,
            persist_directory=chunks_persist_dir,
            embedding_model=embedding_model,
        )

        return self.vectorstore_summary, self.vectorstore_chunks

    
