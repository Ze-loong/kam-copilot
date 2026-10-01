"""
知识库问答子图（F16）自己的 State —— 与主图 State、其他子图 State 都完全独立。

对比 ChatSuggestionState（F02）：结构上高度相似——都是"一次LLM调用产出终态"，
没有 interrupt。区别在于 F16 多了一个检索环节：generate 节点跑之前，
先要用 question 去向量库查出相关片段（retrieved_chunks），再把片段和
问题一起交给LLM组织回答；同时 F16 需要一个"是否命中"的显式判断结果
（hit_knowledge_base），因为 01 号需求文档 3.7.1 节要求"检索不到或
匹配度不达标要走兜底话术"——这个判断结果需要单独存一个字段，
不能只靠 answer_text 是否为空来隐式判断。

设计判断过程见 02 号架构设计文档 7.3 节（知识库RAG设计要点）。
"""

from typing import TypedDict


class KbChunk(TypedDict):
    """单条检索结果片段。"""

    content: str          # 命中的资料原文片段
    source_doc: str       # 来源文档标识（如"集团概况""产品与解决方案-AOI视觉检测系统"）
    score: float           # 相似度分数，用于兜底判定和调试排查


class KbState(TypedDict):
    # 定位信息
    follow_user_id: str
    external_id: str

    # 输入
    question: str                  # 客户原始问题（或综合推理Agent转发的子问题）

    # 检索阶段产出
    retrieved_chunks: list[KbChunk]
    hit_knowledge_base: bool       # 相似度是否达标，决定走据实回答还是兜底话术

    # 生成阶段产出
    answer_text: str               # 最终回答（据实回答 或 兜底话术）
    cited_sources: list[str]       # 回答引用的来源文档列表，对应"每个字来自哪里"这条约束
