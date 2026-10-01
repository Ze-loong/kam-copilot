"""
F16知识库链路最小验证脚本——只验证"装库→建表→摄入→检索"这条工程链路
能不能跑通，不涉及任何LLM生成/prompt设计（那是answer_from_kb的事）。

数据库连接说明：`kam-postgres`容器没有pgvector扩展，实测报错
`FeatureNotSupported`。改为读取`KAM_VECTOR_POSTGRES_URL`这个新环境变量，
指向新建的`kam-postgres-vector`容器（最终改用ankane/pgvector镜像，见
实战记录.md踩坑清单，pgvector/pgvector:pg16因缺postgres系统用户启动失败），
宿主机端口5433。跑之前确认.env里已经有这一行，且该容器已启动。

索引说明：索引类型从`ivfflat`改成`hnsw`。实测发现只有2条
测试数据时，`ivfflat`索引会漏召回（LIMIT 2只返回1条）——这是ivfflat的
已知特性：它靠k-means把向量分聚类簇，默认只探测1个簇，数据量远小于
簇数时容易漏结果，不是脚本或数据的bug。`hnsw`不需要预先聚类训练，
小数据量和大数据量都能正常召回，pgvector 0.5.0+已支持（当前容器实测
0.5.1），本项目资料库规模不大，hnsw更合适，无需额外评估直接换。

跑法：
    cd kam_agent
    uv add sentence-transformers psycopg[binary]   # 如果还没装
    uv run python verify_kb_pipeline.py

预期输出：
    - 打印"pgvector扩展已启用" / "kb_chunks表已就绪"
    - 打印embedding模型下载/加载进度（首次运行会花些时间下载模型）
    - 摄入2条测试资料后，打印检索结果——问"集团成立多少年了"应该命中
      "集团概况"这条，问"AOI 交付周期多久"应该命中"AOI 视觉检测系统"这条

如果哪一步报错，把报错信息发过来，不用自己排查。
"""

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def step1_check_pgvector():
    print("【1/4】检查pgvector扩展与建表...")
    conn_str = os.environ["KAM_VECTOR_POSTGRES_URL"]
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id SERIAL PRIMARY KEY,
                    content TEXT NOT NULL,
                    source_doc TEXT NOT NULL,
                    category TEXT NOT NULL,
                    embedding VECTOR(512),
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx
                    ON kb_chunks USING hnsw (embedding vector_cosine_ops);
                """
            )
        conn.commit()
    print("    OK pgvector扩展已启用，kb_chunks表已就绪\n")


def step2_load_embedding_model():
    print("【2/4】加载embedding模型（首次运行会下载模型文件，需要联网）...")
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
    test_vector = model.encode("测试文本", normalize_embeddings=True)
    print(f"    OK 模型加载成功，向量维度={len(test_vector)}（应为512）\n")
    return model


def step3_ingest_test_data(model):
    print("【3/4】摄入2条测试资料...")
    conn_str = os.environ["KAM_VECTOR_POSTGRES_URL"]

    test_docs = [
        {
            "content": "恒创智能装备集团成立于2011年，总部位于江苏苏州，提供工业自动化解决方案。",
            "source_doc": "集团概况",
            "category": "profile",
        },
        {
            "content": "恒创 AOI 视觉检测系统的典型交付周期为8到10周，具体以打样结果和正式合同为准。",
            "source_doc": "产品与解决方案-AOI视觉检测系统",
            "category": "solution",
        },
    ]

    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            for doc in test_docs:
                cur.execute("DELETE FROM kb_chunks WHERE source_doc = %s", (doc["source_doc"],))
                vector = model.encode(doc["content"], normalize_embeddings=True).tolist()
                cur.execute(
                    """
                    INSERT INTO kb_chunks (content, source_doc, category, embedding)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (doc["content"], doc["source_doc"], doc["category"], vector),
                )
        conn.commit()
    print(f"    OK 已摄入 {len(test_docs)} 条测试资料\n")


def step4_search(model, query: str):
    conn_str = os.environ["KAM_VECTOR_POSTGRES_URL"]
    query_vector = model.encode(query, normalize_embeddings=True).tolist()

    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT content, source_doc, 1 - (embedding <=> %s::vector) AS score
                FROM kb_chunks
                ORDER BY embedding <=> %s::vector
                LIMIT 2
                """,
                (query_vector, query_vector),
            )
            rows = cur.fetchall()

    print(f"    问题：{query}")
    for content, source_doc, score in rows:
        print(f"      [{source_doc}] score={score:.4f}  {content[:40]}...")
    print()


if __name__ == "__main__":
    step1_check_pgvector()
    model = step2_load_embedding_model()
    step3_ingest_test_data(model)

    print("【4/4】检索验证...")
    step4_search(model, "恒创集团成立于哪一年？")
    step4_search(model, "AOI 视觉检测系统的典型交付周期多久？")

    print("全部步骤跑通，链路验证完成。")
