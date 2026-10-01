"""
主图节点函数：意图识别 + 三个"调用子图"的包装节点。

 重构（recreate 多pending interrupt框架边界修复，见 实战记录.md 当天条目）：
generate_all_drafts（一次 LLM 调用生成全部字段草稿）从画像子图上移到这里。
call_profile_subgraph 不再只 invoke 一次子图处理全部字段，改成：先在主图层生成
全部字段草稿，再对每个字段各自发起一次独立 thread_id 的子图 invoke()——
每个子图执行实例从头到尾只处理 1 个字段，永远不会出现"多个字段的 interrupt
同时 pending"这个触发 LangGraph 框架边界的前提条件（根因见
experiments/repro_parallel_dynamic_interrupt.py 组D 的验证结论）。

子 thread_id 命名规则：f"{主 thread_id}:{field_name}"，方便从任意一个子
thread_id 反推回属于哪次生成任务、哪个字段，调试和日志排查时能对应上。
"""

from langgraph.types import Command

from src.graphs.kam_graph.kam_sub_graph_chat_suggestion.sub_chat_suggestion_state import (
    ChatSuggestionState,
)
from src.graphs.kam_graph.kam_sub_graph_kb.sub_kb_state import KbState
from src.graphs.kam_graph.kam_sub_graph_kf_chat_suggestion.sub_kf_chat_suggestion_state import (
    KfChatSuggestionState,
)
from src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_state import ProfileSubState
from src.graphs.kam_graph.kam_sub_graph_tag.sub_tag_state import TagSubState
from src.llm.llm_suggest_profile import suggest_profile_draft
from src.models.kam_models import NEED_VERIFY_THRESHOLD
from src.store.store_client import search_tags_setting

# ：不再在模块顶层直接 import 编译好的 profile_graph（现在没有这个全局对象了）。
# 改为模块级变量占位，由 webapp.py 在应用启动时调用 set_profile_graph() 注入
# 已经带 checkpointer 编译好的子图实例。
_profile_graph = None

# ：标签子图同样走模块级占位 + webapp.py 注入的模式，
# 与 _profile_graph 保持同一套约定。
_tag_graph = None

# ：回复建议两个子图同样走这套约定。这两个子图
# 没有interrupt，但仍然统一由webapp.py注入编译好的实例，保持全项目子图
# 接入方式一致，不因为"这个子图更简单"就搞一套特例。
_chat_suggestion_graph = None
_kf_chat_suggestion_graph = None

# ：知识库问答子图同样没有interrupt，走同一套约定。
_kb_graph = None


def set_profile_graph(compiled_profile_graph) -> None:
    """webapp.py 启动时调用一次，把带 checkpointer 编译好的画像子图注入进来。"""
    global _profile_graph
    _profile_graph = compiled_profile_graph


def set_tag_graph(compiled_tag_graph) -> None:
    """webapp.py 启动时调用一次，把带 checkpointer 编译好的标签子图注入进来。"""
    global _tag_graph
    _tag_graph = compiled_tag_graph


def set_chat_suggestion_graph(compiled_chat_suggestion_graph) -> None:
    """webapp.py 启动时调用一次，注入编译好的销售聊天建议子图。"""
    global _chat_suggestion_graph
    _chat_suggestion_graph = compiled_chat_suggestion_graph


def set_kf_chat_suggestion_graph(compiled_kf_chat_suggestion_graph) -> None:
    """webapp.py 启动时调用一次，注入编译好的客服聊天建议子图。"""
    global _kf_chat_suggestion_graph
    _kf_chat_suggestion_graph = compiled_kf_chat_suggestion_graph


def set_kb_graph(compiled_kb_graph) -> None:
    """webapp.py 启动时调用一次，注入编译好的知识库问答子图（F16）。"""
    global _kb_graph
    _kb_graph = compiled_kb_graph


def detect_intent(state: dict) -> dict:
    """
    意图识别节点：判断这次请求要走画像/标签/回复建议中的哪一个。

    ：不再无条件硬编码返回 "profile"。
    现在 intent 由调用方（webapp.py 对应的 endpoint）在发起 graph.invoke() 时
    显式放进初始 State 里——因为接口层已经用不同的 URL 区分"这是画像请求
    还是标签请求"（08号设计文档判断点：方案A独立接口组），不需要靠这个节点
    去猜或者调 LLM 分类。这里只做两件事：透传 state 里已经有的 intent；如果
    调用方没传（比如旧的画像流程一直没设置这个字段），默认兜底成 "profile"，
    保证画像流程不受影响。等 F02 真正需要"从用户输入自动判断意图"时，再把
    这里升级成读取请求参数或调用 LLM 分类（见 08号设计文档第八节）。
    """
    return {"intent": state.get("intent", "profile")}


def route_by_intent(state: dict) -> str:
    """
    条件边路由函数：根据 state["intent"] 决定走哪个节点名。

     新增 tag 分支（对应 F04）。 新增 chat_suggestion/
    kf_chat_suggestion 两个分支（对应 F02/F03）。 新增
    knowledge_base 分支（对应 F16）——判断依据与 tag 分支相同：

    移除reasoning分支：F17改成直接对reasoning_graph做
    stream_mode="custom"流式调用（见webapp.py `/tasks/stream_reasoning`），
    不再经过主图intent路由——主图节点里"手动invoke整个子图一次拿最终
    结果"这套机制没法把子图内部逐步产生的自定义流事件转发出来，F17
    需要真实的步骤透明推送，只能绕开这层直接访问子图。
    intent 由调用方接口显式传入（不同URL区分销售/客服菜单入口），这里只做
    路由映射，不做任何"猜测该走哪个场景"的判断（见 09号设计文档 判断点3）。
    其余 intent 显式 raise，给上游更精确的错误信息（当前多余，为未来买保险）。
    """
    if state["intent"] == "profile":
        return "profile_subgraph"
    if state["intent"] == "tag":
        return "tag_subgraph"
    if state["intent"] == "chat_suggestion":
        return "chat_suggestion_subgraph"
    if state["intent"] == "kf_chat_suggestion":
        return "kf_chat_suggestion_subgraph"
    if state["intent"] == "knowledge_base":
        return "kb_subgraph"
    raise ValueError(f"暂不支持的 intent: {state['intent']}")


def generate_all_field_drafts(
    wxqy_msgs: list,
    wxkf_msgs: list,
    orders: list,
) -> dict:
    """
    一次 LLM 调用生成全部字段草稿，按 NEED_VERIFY_THRESHOLD 规则标 need_verify。

     从画像子图的 generate_all_drafts 节点搬到这里（主图层）——
    这一步仍然只调用一次 LLM（效率不受影响），只是产出的草稿之后不再交给
    子图内部的 Send 扇出，而是由 call_profile_subgraph 循环发起多次独立
    thread_id 的子图 invoke。
    """
    draft = suggest_profile_draft(
        wxqy_msgs=wxqy_msgs,
        wxkf_msgs=wxkf_msgs,
        orders=orders,
    )

    for field_name, field_data in draft.items():
        if field_data["confidence"] <= NEED_VERIFY_THRESHOLD:
            field_data["status"] = "need_verify"

    return draft


def call_profile_subgraph(state: dict) -> dict:
    """
    包装节点：生成全部字段草稿，再对每个字段各自发起一次独立 thread_id 的
    子图 invoke()，收集每个字段"首次 invoke"产生的 interrupt，一并返回给
    webapp.py 的 SSE 端点去逐条推送。

    返回结构说明：
    - "profile_field_updates"：已经跑完（不常见，MVP 阶段这里应为空，因为
      子图第一次 invoke 必然在 confirm_one_field 处 interrupt，不会直接
      产生终态）
    - "profile_field_interrupts"：每个字段各自 invoke 后拿到的 interrupt
      信息列表，webapp.py 从这里读出来拼 SSE 事件；每一项包含
      field/value/confidence/source/status/interrupt_id/thread_id
      （thread_id 是这个字段自己的子 thread，/tasks/confirm 要用它来定位
      具体是哪个子图执行实例在等待恢复）
    """
    if _profile_graph is None:
        raise RuntimeError(
            "profile_graph 尚未注入——webapp.py 启动时必须先调用 "
            "set_profile_graph() 传入带 checkpointer 编译好的子图实例，"
            "否则 interrupt() 无法真正冻结等待"
        )

    main_thread_id = state["_main_thread_id"]
    wxqy_msgs = state.get("wxqy_msgs", [])
    wxkf_msgs = state.get("wxkf_msgs", [])
    orders = state.get("orders", [])

    draft = generate_all_field_drafts(
        wxqy_msgs=wxqy_msgs,
        wxkf_msgs=wxkf_msgs,
        orders=orders,
    )

    field_interrupts = []
    field_updates = {}

    for field_name, field_data in draft.items():
        field_thread_id = f"{main_thread_id}:{field_name}"
        sub_input: ProfileSubState = {
            "follow_user_id": state["follow_user_id"],
            "external_id": state["external_id"],
            "field_name": field_name,
            "value": field_data["value"],
            "confidence": field_data["confidence"],
            "source": field_data["source"],
            "status": field_data["status"],
            "wxqy_msgs": wxqy_msgs,
            "wxkf_msgs": wxkf_msgs,
            "orders": orders,
            "field_updates": {},
        }

        sub_result = _profile_graph.invoke(
            sub_input,
            config={"configurable": {"thread_id": field_thread_id}},
        )

        sub_interrupts = list(sub_result.get("__interrupt__", ()))
        if sub_interrupts:
            # 单字段子图正常应该只产生 1 条 interrupt（自己这个字段的确认请求）
            interrupt_item = sub_interrupts[0]
            value_data = dict(interrupt_item.value)
            value_data["interrupt_id"] = interrupt_item.id
            value_data["thread_id"] = field_thread_id
            field_interrupts.append(value_data)
        else:
            # 理论上不会走到这里（子图入口就是 confirm_one_field，必然 interrupt）；
            # 一旦出现说明子图执行到了非预期的终点，记录下来但不阻断其他字段处理。
            field_updates.update(sub_result.get("field_updates", {}))

    return {
        "profile_field_updates": field_updates,
        "profile_field_interrupts": field_interrupts,
    }


def call_tag_subgraph(state: dict) -> dict:
    """
    包装节点：整批 invoke 一次标签子图，收集这一条 interrupt 返回给 SSE 端点。

    与 call_profile_subgraph 的关键区别：不循环、不需要为多个推荐各自开
    不同的子thread_id——F04子图本来就只invoke一次、只产生一条interrupt
    （没有recreate，不会触发F01当初踩的"多pending interrupt+动态Send"
    框架边界）。

    ：即便如此，标签子图
    的 thread_id **仍然不能直接等于 main_thread_id**——这是和"要不要按字段
    拆分"完全独立的另一件事，之前把两者混为一谈了。原因：本项目子图不是
    用 LangGraph 官方"挂载子图"的方式接的，而是在普通节点函数里手动调用
    一个独立编译好的 graph 对象的 .invoke()（画像子图、标签子图都是这个
    模式）。这种手动invoke方式下，checkpoint 默认按 (thread_id, "") 存储，
    不会因为"这是哪个 python 对象在调用"自动区分命名空间。如果标签子图
    也用 main_thread_id，它的 checkpoint 链会和**主图自己**的 checkpoint
    链写在完全相同的 (thread_id, "") 位置——主图在 call_tag_subgraph 返回
    后还会继续跑到 END，并把自己的 MainState（没有 add_recommendations
    字段）checkpoint 写到同一个位置，把标签子图刚刚 interrupt 时留下的
    TagSubState checkpoint 覆盖掉。等 /tasks/confirm_tags 再去 resume 时，
    读到的已经是被主图覆盖过的 checkpoint，add_recommendations 自然是空的
    ——这正是真实联调时复现的现象。

    修复：给标签子图的 thread_id 加一个和主图不同的后缀（f"{main_thread_id}:tag"），
    只是为了让两条 checkpoint 链不撞在一起，不是为了区分"多个执行实例"
    （F04始终只有一个执行实例，这点和F01的"每字段一个"不同，后缀原因也
    不同，不要混淆）。画像子图当初用 f"{main_thread_id}:{field_name}" 时，
    这条"必须和主图thread_id不同"的要求恰好被顺带满足了，所以没暴露过
    这个问题；F04这里是因为没有多字段可拼、想省事直接用了main_thread_id，
    才踩到这个之前没被验证过的坑。

    current_tags 从 state["external_user"]（load_data 节点已经查好、整个
    塞进主图State）里取；tag_catalog 直接调 search_tags_setting() 拿 Store
    原始 Item 列表，不能先拍平成 [item.value ...]——tag_id 只在 Item.key
    上，这是 08 号设计文档记录过的一次真实踩坑（编写 llm_suggest_tag.py
    时先发现的）。
    """
    if _tag_graph is None:
        raise RuntimeError(
            "tag_graph 尚未注入——webapp.py 启动时必须先调用 "
            "set_tag_graph() 传入带 checkpointer 编译好的子图实例，"
            "否则 interrupt() 无法真正冻结等待"
        )

    main_thread_id = state["_main_thread_id"]
    external_user = state.get("external_user")
    current_tags = external_user.value["tags"] if external_user else []

    # ：不能把 search_tags_setting()
    # 返回的原始 SearchItem 对象列表直接塞进 TagSubState——TagSubState 是
    # 会被 LangGraph checkpoint 的图 State，塞进未注册类型的自定义对象，
    # resume 时反序列化会报 "Deserializing unregistered type ... from
    # checkpoint" 警告，且不保证反序列化后的对象还能正常支持
    # _normalize_tag_catalog() 里的 .key/.value 鸭子类型判断。在这里、
    # State 边界之前就转成纯 dict（同时保留 tag_id，做法和 webapp.py 里
    # /store/* 接口的 _item_to_dict() 是同一个思路：Store 对象不能带着
    # 跨越"要被持久化/序列化"的边界，要先落地成普通数据结构）。
    tag_catalog = [
        {"tag_id": item.key, **item.value}
        for item in search_tags_setting()
        if hasattr(item, "key") and hasattr(item, "value")
    ]

    sub_input: TagSubState = {
        "follow_user_id": state["follow_user_id"],
        "external_id": state["external_id"],
        "current_tags": current_tags,
        "tag_catalog": tag_catalog,
        "wxqy_msgs": state.get("wxqy_msgs", []),
        "wxkf_msgs": state.get("wxkf_msgs", []),
        "orders": state.get("orders", []),
        "add_recommendations": [],
        "remove_recommendations": [],
        "confirmed_add_tag_ids": [],
        "confirmed_remove_tag_ids": [],
    }

    # 必须和 main_thread_id 不同（见上方  修正说明），否则标签
    # 子图的 checkpoint 会和主图自己的 checkpoint 撞在同一个 (thread_id, "")
    # 位置，被主图跑到 END 时写的 MainState checkpoint 覆盖掉。不需要按
    # 字段再拆分（F04只有一次invoke），固定加 ":tag" 后缀即可。
    tag_thread_id = f"{main_thread_id}:tag"

    sub_result = _tag_graph.invoke(
        sub_input,
        config={"configurable": {"thread_id": tag_thread_id}},
    )

    tag_interrupt = None
    sub_interrupts = list(sub_result.get("__interrupt__", ()))
    if sub_interrupts:
        interrupt_item = sub_interrupts[0]
        tag_interrupt = dict(interrupt_item.value)
        tag_interrupt["interrupt_id"] = interrupt_item.id
        # 前端后续 /tasks/confirm_tags 要用这个 thread_id 去 resume，必须是
        # 标签子图自己的 tag_thread_id，不是主图的 main_thread_id。
        tag_interrupt["thread_id"] = tag_thread_id

    return {"tag_interrupt": tag_interrupt}


def call_chat_suggestion_subgraph(state: dict) -> dict:
    """
    包装节点：invoke一次销售聊天回复建议子图，结果直接放进 task_result。

    与 call_tag_subgraph 的关键区别：没有 interrupt，不需要检查
    sub_result.get("__interrupt__")、不需要收集 interrupt_id/thread_id——
    invoke() 返回的就是最终结果，直接取出来即可。也不需要拼子thread_id，
    直接用主thread_id（没有暂停就没有"checkpoint被覆盖"的风险，见
    09号设计文档 判断点2）。
    """
    if _chat_suggestion_graph is None:
        raise RuntimeError(
            "chat_suggestion_graph 尚未注入——webapp.py 启动时必须先调用 "
            "set_chat_suggestion_graph() 传入编译好的子图实例"
        )

    main_thread_id = state["_main_thread_id"]

    sub_input: ChatSuggestionState = {
        "follow_user_id": state["follow_user_id"],
        "external_id": state["external_id"],
        "external_user": state.get("external_user"),
        "profile": state.get("profile"),
        "wxqy_msgs": state.get("wxqy_msgs", []),
        "orders": state.get("orders", []),
        "suggestion_text": "",
        "reasoning": "",
    }

    sub_result = _chat_suggestion_graph.invoke(
        sub_input,
        config={"configurable": {"thread_id": main_thread_id}},
    )

    return {
        "task_result": {
            "type": "chat_suggestion",
            "suggestion_text": sub_result["suggestion_text"],
            "reasoning": sub_result["reasoning"],
        }
    }


def call_kf_chat_suggestion_subgraph(state: dict) -> dict:
    """
    包装节点：invoke一次客服聊天回复建议子图，结果直接放进 task_result。

    老客应答使用客服聊天和订单记录，不传销售画像。
    """
    if _kf_chat_suggestion_graph is None:
        raise RuntimeError(
            "kf_chat_suggestion_graph 尚未注入——webapp.py 启动时必须先调用 "
            "set_kf_chat_suggestion_graph() 传入编译好的子图实例"
        )

    main_thread_id = state["_main_thread_id"]

    sub_input: KfChatSuggestionState = {
        "follow_user_id": state["follow_user_id"],
        "external_id": state["external_id"],
        "external_user": state.get("external_user"),
        "wxkf_msgs": state.get("wxkf_msgs", []),
        "orders": state.get("orders", []),
        "suggestion_text": "",
        "reasoning": "",
    }

    sub_result = _kf_chat_suggestion_graph.invoke(
        sub_input,
        config={"configurable": {"thread_id": main_thread_id}},
    )

    return {
        "task_result": {
            "type": "kf_chat_suggestion",
            "suggestion_text": sub_result["suggestion_text"],
            "reasoning": sub_result["reasoning"],
        }
    }


def call_kb_subgraph(state: dict) -> dict:
    """
    包装节点：invoke一次知识库问答子图（F16），结果直接放进 task_result。

    结构与 call_chat_suggestion_subgraph 一致（没有interrupt，一次invoke
    拿到终态）。与F02/F03的关键区别：F16不依赖 load_data 产出的
    external_user/profile/wxqy_msgs/orders——F16只看客户这次问的问题
    本身，不看客户历史数据，所以 sub_input 不从 state 里取那几个字段，
    只取 follow_user_id/external_id/question 三个。question 由调用方
    （webapp.py 对应的 endpoint）在发起主图 invoke() 时放进初始 State，
    和 intent 的传入方式一致。
    """
    if _kb_graph is None:
        raise RuntimeError(
            "kb_graph 尚未注入——webapp.py 启动时必须先调用 "
            "set_kb_graph() 传入编译好的子图实例"
        )

    main_thread_id = state["_main_thread_id"]

    sub_input: KbState = {
        "follow_user_id": state["follow_user_id"],
        "external_id": state["external_id"],
        "question": state["question"],
        "retrieved_chunks": [],
        "hit_knowledge_base": False,
        "answer_text": "",
        "cited_sources": [],
    }

    sub_result = _kb_graph.invoke(
        sub_input,
        config={"configurable": {"thread_id": main_thread_id}},
    )

    return {
        "task_result": {
            "type": "kb_answer",
            "answer_text": sub_result["answer_text"],
            "cited_sources": sub_result["cited_sources"],
        }
    }
