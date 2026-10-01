"""
知识库问答子图（F16）的节点函数。

与F02（sub_chat_suggestion）的关键区别：F02只有一个节点（generate），
F16需要两个节点——先 retrieve（检索），再 generate（组织回答）。
拆成两个节点而不是揉进一个函数里，是为了让"检索"这个可复用能力
将来能被F17（综合推理）的 execute_step 节点直接调用（见02号架构
设计文档7.1节"F17内部调用F16"）——检索逻辑只写一份，装配成LangGraph
节点是这里，装配成普通函数供F17调用是另一回事，核心检索代码在
src/kb/kb_retriever.py里，两处都从那里导入。

retrieve_kb_chunks 负责检索与阈值判断；generate_kb_answer 将片段交给
llm_answer_kb.py 据实回答。无法回答时，固定兜底话术嵌入顾问姓名。
"""

from src.graphs.kam_graph.kam_sub_graph_kb.sub_kb_state import KbState
from src.kb.kb_retriever import search_knowledge_base
from src.llm.llm_answer_kb import answer_from_kb

# 相似度阈值：低于此值判定"未命中"，走兜底话术。
#
# 用真实摄入的四类资料（集团概况/产品与解决方案/资质与标杆案例/常见FAQ）+
# 13个代表性问题（含改述、边界案例、完全不相关案例）实测调整，从占位值
# 0.5改成0.4：真实相关问题的最低分是0.4347（"你们公司成立多久了"这种
# 改述问法），完全不相关问题的最高分是0.3490（"今天股票涨了吗"），0.4
# 卡在这两者中间，留了安全余量。
#
# 阈值选得偏"宽松"（宁可多让一些边界案例过阈值，也不轻易漏掉真实相关
# 问题）是有意的权衡：漏判（真的相关但被判成"未命中"）会直接走死板的
# 兜底话术，完全没有挽回余地；而误判（不太相关但被判成"命中"）还有
# generate_kb_answer的CoT兜底——system_prompt里明确要求"如果片段其实
# 答不上问题，如实说明资料库中未找到"，不会强行编答案。两种错误的
# 代价不对称，所以阈值往"宁可信其相关"的方向偏。
#
# 换成制造业资料后复测（8 个问题）：相关问题最低分 0.5715（"有没有做过锂电
# 行业的案例"），不相关问题"能不能做 ERP 财务系统"得分 0.5407、"公司股票
# 代码"0.4517——两类分数已经交叉到只差 0.03，任何单一阈值都分不干净。
# 所以保持宽松阈值不动，把"答不上"的判断交给第二道关：模型逐条阅读片段后
# 返回 answerable=false，代码统一改走兜底话术（见 llm_answer_kb.py）。
# 阈值负责粗筛明显无关的问题，模型负责精判，兜底话术由代码固定输出。
#
# 诚实说明局限性：这次调优只测了13个问题，不是严格的精确率/召回率
# 统计验证（那需要几十上百条人工标注样本），是"比纯拍脑袋的占位值
# 更靠谱"的一次改进，不是"已经调到最优"。如果后续真实使用中发现
# 兜底率（01号需求文档3.7.3节验收指标）持续偏高或偏低，需要用真实
# 用户问题重新评估这个数字。
KB_HIT_THRESHOLD = 0.4


def retrieve_kb_chunks(state: KbState) -> dict:
    """
    检索节点：拿 question 去向量库查相关片段。

    search_knowledge_base() 内部调用细节见 src/kb/kb_retriever.py，
    这里只负责组装调用、判定是否命中阈值。
    """
    chunks = search_knowledge_base(
        query=state["question"],
        top_k=3,
    )
    hit = bool(chunks) and chunks[0]["score"] >= KB_HIT_THRESHOLD

    return {
        "retrieved_chunks": chunks,
        "hit_knowledge_base": hit,
    }


def generate_kb_answer(state: KbState) -> dict:
    """
    生成节点：命中则据实组织回答，未命中则走兜底话术。

    answer_from_kb() 负责据实回答和固定兜底话术；节点只组装入参/取出结果。
    """
    result = answer_from_kb(
        question=state["question"],
        chunks=state["retrieved_chunks"],
        hit_knowledge_base=state["hit_knowledge_base"],
        follow_user_id=state["follow_user_id"],  # 兜底话术需嵌入顾问姓名，
                                                    # 顾问姓名由answer_from_kb内部
                                                    # 查employee命名空间获取，
                                                    # 这里只透传follow_user_id
    )
    return {
        "answer_text": result["answer_text"],
        "cited_sources": result["cited_sources"],
    }
