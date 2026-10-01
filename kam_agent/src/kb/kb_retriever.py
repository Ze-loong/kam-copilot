"""
知识库向量检索（对应02号架构设计文档7.3节
"向量库选型：沿用一期PostgreSQL的pgvector扩展，不引入新组件"，
但实测发现一期的PostgreSQL实例没有预装pgvector，见下方"数据库连接"说明）。

****：此前版本假设embedding走阿里云百炼（DashScope）API，
但项目.env里实际只有DEEPSEEK_API_KEY，DeepSeek官方不提供embedding接口。
改为本地开源模型（sentence-transformers + BAAI/bge-small-zh-v1.5），
不需要额外API Key，首次运行会自动从HuggingFace下载模型文件（约100MB），
之后离线可用。

**（数据库连接）**：一期的`kam-postgres`容器用标准PostgreSQL
镜像，没有pgvector扩展，实测`CREATE EXTENSION vector`直接报错
`FeatureNotSupported`。改为新建独立的`kam-postgres-vector`容器
（`pgvector/pgvector:pg16`镜像，宿主机端口5433，避免和现有5432冲突），
专门存知识库向量数据，不改动一期已有的`kam-postgres`容器（里面有一期
mock数据，不想有任何风险）。因此本文件读取的环境变量是
**`KAM_VECTOR_POSTGRES_URL`，不是`KAM_POSTGRES_URL`**——后者是一期业务
数据库，前者是二期知识库专用的向量数据库，两个是不同的PostgreSQL实例。

这份文件被两处调用：
1. sub_kb_node.py 的 retrieve_kb_chunks 节点（F16独立问答场景）
2. 后续F17的 execute_step 节点在 tool == "knowledge_base" 时
   （综合推理场景，见02号架构设计文档7.1节"F17内部调用F16的RAG检索函数"）
两处调用同一份检索逻辑，不重复实现。

前置条件：
1. `kam-postgres-vector`容器已启动（pgvector扩展内置于镜像，不需要手动装）
2. `.env`里已配置`KAM_VECTOR_POSTGRES_URL`
3. 已安装 sentence-transformers：uv add sentence-transformers
4. 已通过资料摄入脚本（kb_ingest.py）把四类官方资料切分、向量化后
   写入了下面这张表
"""

from __future__ import annotations

import os
from functools import lru_cache

import psycopg

CONNECT_TIMEOUT_SECONDS = 3

# BAAI/bge-small-zh-v1.5：中文场景常用的轻量开源embedding模型，
# 输出向量维度512，模型体积小（约95MB），CPU上跑也够快，适合本项目
# mock数据规模（四类官方资料，量级不大）。如果后续发现检索效果不理想，
# 可换成bge-base-zh-v1.5（效果更好但更慢，向量维度768需同步改建表SQL）。
_EMBEDDING_MODEL_NAME = "BAAI/bge-small-zh-v1.5"


@lru_cache
def _get_embedding_model():
    """
    懒加载+缓存embedding模型，避免每次调用search_knowledge_base都重新加载。

    ：
    SentenceTransformer默认即使本地已有缓存，实例化时仍会尝试联网核对
    HuggingFace Hub上的文件版本，不是"首次下载后自动离线"——本机对
    huggingface.co的访问不稳定时，这一步会挂起数十秒甚至更久（实测卡住
    超过1分钟，f16/f17此前的验证之所以没暴露这个问题，只是因为当时那次
    网络恰好通畅）。改成优先尝试local_files_only=True（模型确实已缓存在
    本地，见~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5），
    只有本地没有缓存（全新环境第一次跑）时才退回联网下载，这样"下载完成后
    即使断网也能继续使用"这句话才是真的成立。
    """
    # sentence-transformers 会连带导入 torch，这两个包初始化较重。
    # 只有真正做向量检索/摄入时才需要，不应拖慢普通 API 和测试收集。
    from sentence_transformers import SentenceTransformer

    try:
        return SentenceTransformer(_EMBEDDING_MODEL_NAME, local_files_only=True)
    except OSError:
        return SentenceTransformer(_EMBEDDING_MODEL_NAME)


def _embed(text: str) -> list[float]:
    """把一段文本转成向量，用于查询/摄入时的embedding。"""
    return _embed_many([text])[0]


def _embed_many(texts: list[str]) -> list[list[float]]:
    """一次批量编码多个文本，避免摄入时逐片段调用模型。"""
    if not texts:
        return []
    model = _get_embedding_model()
    vectors = model.encode(texts, normalize_embeddings=True)
    return vectors.tolist()


def search_knowledge_base(
    query: str, top_k: int = 3, category: str | None = None
) -> list[dict]:
    """
    用query去pgvector表里做相似度检索，返回最相关的top_k条片段。

    返回格式对应 sub_kb_state.py 里的 KbChunk：
    [{"content": ..., "source_doc": ..., "score": ...}, ...]
    按score降序排列（越大越相似，用余弦相似度）。

    category 参数供 F17 solution_catalog 工具复用：这个信息入口检索
    同一张知识库表里 category="solution" 的内容，无须另建检索函数。
    不传时（None）行为与原来完全一致，F16原有调用方不受影响。
    """
    query_vector = _embed(query)

    conn_str = os.environ["KAM_VECTOR_POSTGRES_URL"]
    with psycopg.connect(conn_str, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        with conn.cursor() as cur:
            # 1 - (embedding <=> 查询向量) 把pgvector的"距离"转成"相似度"，
            # <=> 是pgvector的余弦距离操作符，距离越小越相似，
            # 转成相似度（1-距离）方便和KB_HIT_THRESHOLD这类"越大越好"的阈值对齐。
            # normalize_embeddings=True已保证向量是单位向量，
            # 余弦距离和余弦相似度的转换关系才严格成立。
            if category:
                cur.execute(
                    """
                    SELECT content, source_doc, 1 - (embedding <=> %s::vector) AS score
                    FROM kb_chunks
                    WHERE category = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (query_vector, category, query_vector, top_k),
                )
            else:
                cur.execute(
                    """
                    SELECT content, source_doc, 1 - (embedding <=> %s::vector) AS score
                    FROM kb_chunks
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (query_vector, query_vector, top_k),
                )
            rows = cur.fetchall()

    return [
        {"content": row[0], "source_doc": row[1], "score": float(row[2])}
        for row in rows
    ]
