"""
标签推荐子图自己的 State —— 与主图 State、画像子图 State 都完全独立。

对比 ProfileSubState：没有 confidence/status 字段（标签是"贴/不贴"二元判断，
不是置信度判断），没有逐字段循环用的 field_updates 字典（F04是整批一次
interrupt、整批一次提交，不是逐项确认）。设计判断过程见
设计文档/08-F04标签推荐设计文档.md 第三节。
"""

from typing import TypedDict


class TagRecommendation(TypedDict):
    """单条标签推荐的结构：标签ID/名称 + 推荐理由，没有置信度字段。"""
    tag_id: str
    tag_name: str
    reason: str


class TagSubState(TypedDict):
    # 定位信息：写回 Store 时需要
    follow_user_id: str
    external_id: str

    # 生成推荐时需要的素材：客户当前标签 + 标签体系全集 + 客户行为数据
    # 注意：current_tags 是纯 tag_id 字符串列表；tag_catalog 是纯 dict 列表
    # （形如 {"tag_id": ..., "tag_name": ..., ...}），不是 search_tags_setting()
    # 原样返回的 SearchItem 对象列表。
    #
    # 这里的字段类型经历过两次踩坑修正：①最初以为可以直接拍平成
    # [item.value ...]，会丢掉 tag_id（tag_id 只在 Item.key 上，见 08 号
    # 设计文档第四节）；②后来直接把原始 SearchItem 对象列表塞进本 State，
    # 又踩到"TagSubState 会被 LangGraph checkpoint"这件事——未注册的自定义
    # 类型进入图 State，resume 时反序列化会报 "Deserializing unregistered
    # type" 警告（ 真实联调复现）。现在的做法是在 State 边界之前
    # （kam_node.py 的 call_tag_subgraph 里）就把 SearchItem 转成纯 dict、
    # 同时用 {"tag_id": item.key, **item.value} 保留住 tag_id，两个问题
    # 一次性避开：State 里只有 JSON 原生可序列化的结构，tag_id 也没丢。
    current_tags: list[str]
    tag_catalog: list[dict]
    wxqy_msgs: list
    wxkf_msgs: list
    orders: list

    # LLM生成的推荐结果，整批interrupt时作为value的一部分推给前端
    add_recommendations: list[TagRecommendation]
    remove_recommendations: list[TagRecommendation]

    # 用户勾选后提交、经后端校验过的最终结果：只有"哪些标签id最终生效"
    confirmed_add_tag_ids: list[str]
    confirmed_remove_tag_ids: list[str]
