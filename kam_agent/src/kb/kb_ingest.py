"""
资料库摄入脚本。

用途：把四类演示资料（集团概况/产品与解决方案/资质与标杆案例/FAQ）切分、向量化后
写入pgvector表；也是 /kb/upload 接口（02号架构设计文档7.4节）内部
应该调用的核心逻辑，这里先做成独立脚本方便开发阶段手动跑通验证，
后续webapp.py里的路由直接复用这个函数即可。

切分策略：这里给的是最基础的"按段落切分+固定长度兜底"，02号文档
7.3节已经说明"四类资料内容结构差异较大，具体切分策略留待开发阶段
针对四类资料分别设计"——这份先跑通链路，真正的切分策略优化
（比如FAQ按问答对切、解决方案按产品切）留给你实测调优。

首次运行前需要建表（在数据库里手动执行一次，或加进项目的建表脚本）：

    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE IF NOT EXISTS kb_chunks (
        id SERIAL PRIMARY KEY,
        content TEXT NOT NULL,
        source_doc TEXT NOT NULL,       -- 来源文档标识，如"集团概况"
        category TEXT NOT NULL,         -- 资料类别：profile/solution/honor/faq
        embedding VECTOR(512),          -- 512 = BAAI/bge-small-zh-v1.5 的输出维度，
                                          -- 换模型（如bge-base-zh-v1.5是768维）必须
                                          -- 同步改这里，维度不匹配插入会直接报错
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx
        ON kb_chunks USING hnsw (embedding vector_cosine_ops);

：embedding从DashScope改为本地开源模型（见kb_retriever.py顶部说明），
这里的向量维度同步从1024改成512。

：上面的建表SQL要在**新建的`kam-postgres-vector`容器**
（KAM_VECTOR_POSTGRES_URL指向的实例）里执行，不是一期的`kam-postgres`
容器——一期容器没有pgvector扩展。详见kb_retriever.py顶部"修正2"说明。

：索引类型从ivfflat改成hnsw——实测ivfflat在数据量很小时
（验证脚本里只有2条测试数据）会漏召回，hnsw不需要聚类训练、小数据量
也能正常工作，pgvector 0.5.0+支持（当前容器0.5.1），详见verify_kb_pipeline.py
顶部"修正2"说明。
"""

import os

import psycopg

from src.kb.kb_retriever import CONNECT_TIMEOUT_SECONDS, _embed_many  # 摄入时批量复用同一模型


def _split_into_chunks(text: str, max_length: int = 500) -> list[str]:
    """
    最基础的切分：按空行分段，超长段落再按max_length硬切。
    这是占位实现，真正的策略优化留给开发阶段针对四类资料分别处理
    （见本文件顶部说明），先保证链路能跑通。

    ：先把 \\r\\n / \\r 统一normalize成 \\n 再按"\\n\\n"切分——
    浏览器textarea提交多行内容时惯例用\\r\\n换行（kam_admin"上传/替换资料"
    模态框真实测试时触发，见任务清单.md"技术备忘"），不做normalize的话
    "\\r\\n\\r\\n"匹配不上字面量"\\n\\n"，整篇文档会被当成一个段落，不切分，
    检索粒度直接从"按段落"退化成"按整篇文档"，明显伤检索质量。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    for p in paragraphs:
        if len(p) <= max_length:
            chunks.append(p)
        else:
            for i in range(0, len(p), max_length):
                chunks.append(p[i : i + max_length])
    return chunks


def ingest_document(text: str, source_doc: str, category: str) -> int:
    """
    把一份资料文档切分、向量化、写入pgvector表。

    调用方（/kb/upload接口）负责先删除该source_doc的旧片段（替换语义，
    对应01号文档"运营人员上传替换、一键生效"），这里只负责写入新片段。
    返回写入的片段数量，供接口返回给前端确认。
    """
    chunks = _split_into_chunks(text)
    # 一次 encode 整批片段，避免每个片段都单独进入模型。
    vectors = _embed_many(chunks)

    conn_str = os.environ["KAM_VECTOR_POSTGRES_URL"]
    with psycopg.connect(conn_str, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        with conn.cursor() as cur:
            # 替换语义：先删同一来源文档的旧片段，再插入新的
            cur.execute("DELETE FROM kb_chunks WHERE source_doc = %s", (source_doc,))

            for chunk_text, vector in zip(chunks, vectors, strict=True):
                cur.execute(
                    """
                    INSERT INTO kb_chunks (content, source_doc, category, embedding)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (chunk_text, source_doc, category, vector),
                )
        conn.commit()

    return len(chunks)


def list_documents() -> list[dict]:
    """
    查询知识库当前已摄入的文档列表，按 (category, source_doc) 分组统计片段数
    和最后更新时间，供 kam_admin 的资料库管理页面（02号架构文档7.4节
    `/kb/list`）展示。纯查询，不涉及embedding，比 ingest_document 简单。
    """
    conn_str = os.environ["KAM_VECTOR_POSTGRES_URL"]
    with psycopg.connect(conn_str, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT category, source_doc, COUNT(*) AS chunk_count, MAX(created_at) AS updated_at
                FROM kb_chunks
                GROUP BY category, source_doc
                ORDER BY category, source_doc
                """
            )
            rows = cur.fetchall()

    return [
        {
            "category": row[0],
            "source_doc": row[1],
            "chunk_count": row[2],
            "updated_at": row[3].isoformat() if row[3] else None,
        }
        for row in rows
    ]


def get_document_content(source_doc: str) -> str | None:
    """
    查询某一份文档的完整内容——把所有片段按插入顺序（id自增）用"\n\n"
    重新拼接回一段文本，供 kam_admin 资料库管理页"查看"功能展示、以及
    "查看后一键把内容灌进上传框、改完直接重传"这条复用上传接口的编辑
    路径使用（）。跟 list_documents() 的区别：那是跨文档的
    分组统计（不含content），这是单文档的完整内容，两者查询目的不同，
    不适合合并成一个接口。

    返回 None 表示这个 source_doc 不存在（未摄入过或已被删除）。

    注意：拼接结果不保证与原始上传文本逐字节一致——_split_into_chunks
    对超长段落会硬切，"\n\n"拼接后硬切处的原始换行边界会丢失——但足够
    满足"看看这份资料大致写了什么/复制出来改一改再传"这两个使用场景，
    不是要做逐字节还原。
    """
    conn_str = os.environ["KAM_VECTOR_POSTGRES_URL"]
    with psycopg.connect(conn_str, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content FROM kb_chunks WHERE source_doc = %s ORDER BY id",
                (source_doc,),
            )
            rows = cur.fetchall()

    if not rows:
        return None
    return "\n\n".join(row[0] for row in rows)


if __name__ == "__main__":
    # 手动验证用：把某个资料文件摄入知识库。
    # 真实开发时替换成从上传的实际文件读取内容。
    sample_text = """集团概况

恒创智能装备集团成立于2011年，面向离散制造业提供工业自动化解决方案。

本资料为虚构演示内容。"""

    count = ingest_document(
        text=sample_text,
        source_doc="集团概况",
        category="profile",
    )
    print(f"已写入 {count} 条片段")
