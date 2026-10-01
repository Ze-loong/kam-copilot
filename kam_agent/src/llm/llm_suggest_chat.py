"""F02/F03 智能聊天回复建议的 LLM 调用与结果校验。

设计判断过程见 设计文档/09-F02F03回复建议设计文档.md 第五节。

与画像(F01)/标签(F04)的关键区别：这里不产出需要人工确认的候选项（没有
confidence/tag_id这类需要校验合法性的结构化字段），LLM输出的是自然语言
建议文本本身就是最终结果，不需要白名单过滤——但仍然需要校验"LLM是否
老实按格式输出了两个字段"，防止JSON解析出来缺字段导致下游报错。

两个函数共用同一套"先诊断、再建议"的CoT结构（抄F04当初"先做证据梳理、
再输出JSON"验证有效的思路，见08号设计文档mock数据调优记录），但诊断
的判断维度完全不同：
- 新客方案沟通（销售）诊断"商机处于哪个阶段、客户卡点是什么、对方在决策链中的角色"；
- 老客售后与扩容（客服）诊断"这是什么类型的问题、紧急程度多高、涉及哪台已采购设备"。

两者的信息来源也不同：销售场景依赖客户画像（技改需求、决策链）+ 企微聊天；
客服场景依赖订单记录（交付与维保历史）+ 微信客服聊天。
"""

import json
import os
import re
import sys

from src.llm.llm_setting import get_llm
from src.llm.json_retry import invoke_and_parse_json

_DEBUG_LLM_RAW = os.environ.get("KAM_DEBUG_LLM_RAW") == "1"


def _extract_json_block(text: str) -> dict:
    """从 LLM 回复中取出 JSON 代码块；没有代码块时按整段 JSON 解析。

    与 llm_suggest_tag.py 的同名函数完全一致，复用同一个正则约定
    （```json 代码块优先，没有则整段尝试解析）。
    """
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    json_text = match.group(1) if match else text
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError("DeepSeek 返回的不是合法 JSON") from error


def _items_to_text(items: list) -> str:
    """把 Store Item 列表或普通 dict 列表转换为可放入 prompt 的 JSON。

    与 llm_suggest_tag.py 的同名函数完全一致——两个文件都要处理
    "Store Item 对象列表" vs "纯 dict 列表"两种输入形态，逻辑没有
    随业务场景变化的理由，直接复用同一实现。
    """
    if not items:
        return "无"
    values = [item.value if hasattr(item, "value") else item for item in items]
    return json.dumps(values, ensure_ascii=False, default=str)


def _solution_refs_to_text(solution_refs: list | None) -> str:
    """把检索到的产品方案片段整理成 prompt 文本；没有片段时明确写"无"。"""
    if not solution_refs:
        return "无（未检索到相关资料）"
    return "\n\n".join(
        f"[来源：{ref['source_doc']}]\n{ref['content']}" for ref in solution_refs
    )


def _validate_suggestion_payload(payload: dict) -> dict[str, str]:
    """校验LLM输出是否老实给了 suggestion_text/reasoning 两个非空字符串字段。

    这里不做白名单过滤（不像标签推荐要校验tag_id是否在合法范围内）——
    回复建议是自由文本，没有"合法值域"这个概念，唯一要防的是LLM没有
    按格式输出（缺字段、字段是空字符串、字段类型不对），这些情况会让
    下游 webapp.py 接口拿到 None 或空值，需要在这里挡住、显式报错，
    而不是让脏数据流到接口响应里。
    """
    if not isinstance(payload, dict):
        raise ValueError("DeepSeek 返回的回复建议必须是 JSON 对象")

    suggestion_text = payload.get("suggestion_text")
    reasoning = payload.get("reasoning")

    if not isinstance(suggestion_text, str) or not suggestion_text.strip():
        raise ValueError("DeepSeek 未返回有效的 suggestion_text")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("DeepSeek 未返回有效的 reasoning")

    return {
        "suggestion_text": suggestion_text.strip(),
        "reasoning": reasoning.strip(),
    }


def suggest_chat_reply(
    external_user: dict | None,
    profile: dict | None,
    wxqy_msgs: list,
    orders: list,
    solution_refs: list | None = None,
) -> dict[str, str]:
    """F02.1 销售聊天回复建议。

    新客方案沟通：聚焦推动商机前进的话术建议（方案答疑、价格沟通、
    痛点回应、决策链推动）。所需数据输入：聊天历史、订单历史、
    客户信息（含标签）、客户画像，以及按客户近期聊天内容从知识库
    "产品与解决方案"类资料中检索到的片段（solution_refs）。
    """
    external_user_value = external_user.value if hasattr(external_user, "value") else external_user
    profile_value = profile.value if hasattr(profile, "value") else profile

    customer_text = json.dumps(external_user_value, ensure_ascii=False, default=str) if external_user_value else "无客户基本信息"
    profile_text = json.dumps(profile_value, ensure_ascii=False, default=str) if profile_value else "无画像信息（尚未生成或未确认任何字段）"

    system_prompt = """
你是制造业大客户销售顾问的回复建议助手。销售顾问正在通过企业微信与制造企业客户
沟通产线技改方案（新客或尚在推进中的商机），你的任务是分析当前对话，
生成一条可以直接参考或修改后发送的回复建议，附带推理说明。

# 分析步骤（必须先做，再给建议，不能跳过）
1. 先诊断"商机当前所处阶段"，只能是以下四类之一：
   - 需求了解：刚接触，客户在了解我方能力、案例、大致方案
   - 方案评估：客户在比较技术方案、设备参数、多家供应商，或对价格/方案提出具体疑问
   - 立项推动：客户认可方案方向，但卡在内部审批、预算申请或决策链上的某个人
   - 临门一脚：已进入合同条款、付款方式、交期、验收标准等细节确认
2. 再诊断"当前对话中客户最主要的顾虑或诉求"，用一句话概括
   （例如"担心改造期间停线影响订单交付""投资回报周期说服不了老板""想要更低的价格"）。
3. 查看"产品与解决方案资料"片段，判断其中是否有能直接回应客户当前关注点的内容
   （标准配置、适用场景、样机测试政策、典型交付周期等）。有就在回复里准确引用，没有就不引用。
4. 结合客户画像中的决策链，判断这次对话的对象在决策链里是什么角色
   （提需求的使用部门 / 把技术关的人 / 最终拍板人 / 采购），
   回复要给对方真正需要的东西：技术把关人要参数和同类案例，采购要报价构成和交期，
   拍板人要投资回报和风险控制。画像里没有决策链信息时，按对话内容谨慎判断，不要编造。
5. 基于以上判断给出针对性的回复建议——不要给通用模板话术，建议必须能看出是
   "回应了这次对话里客户具体说的内容"。

# 建议内容要求
- 语气专业、务实，符合 B2B 工业客户的沟通习惯，避免过度营销话术堆砌。
- 如果客户画像或订单信息里有可以自然带入的具体信息（产线现状、节拍/良率目标、预算区间、已采购设备），
  优先使用，让建议更有针对性；没有相关信息时不要编造。
- 回复建议长度控制在可以直接复制发送的长度（2-4句话为宜），不要写成一大段说明文。
- 不要在 suggestion_text 里出现"建议您可以这样回复"这类元话术，suggestion_text 就是可以直接发给客户的话本身。

# 事实边界（硬约束，不允许违反）
- 产品与解决方案资料只能按原意引用；资料里的交付周期、节拍等是通用参考值，
  引用时必须保留"典型""参考""以现场勘查和正式合同为准"之类的限定，不能说成对这个客户的承诺。
  资料里没有的产品能力，不能因为客户问了就说"可以"。
- 不得替我方承诺具体价格、折扣、交期、性能指标（节拍、良率提升幅度、投资回收期等），
  除非聊天记录里我方已经明确给出过同样的数字；需要这类信息时，改用
  "我请技术同事根据现场情况核算后，给您出正式方案"这类表述。
- 订单记录只能证明"客户采购了某项设备或服务"，不能证明它的执行状态（是否已到厂、已调试、已验收、运行效果如何）。
  未经聊天记录或客户画像明确文字确认前，一律视为"执行状态未知"，
  不能默认假设已完成，也不能默认假设未开始——两个方向的假设都不允许。
- 不得使用任何默认"已经发生"的完成态措辞，包括但不限于："用下来怎么样""验收后""调试完""效果出来了"
  "已整理出""已生成""已完成"等。这些表述只有在聊天记录或客户画像里有明确文字确认该事件已发生时才能使用。
- 信息不足时，必须改用条件式或开放式问法，把"是否已发生"留给客户确认，例如：
  "如果设备已经到厂调试，方便和我说说现场运行情况吗？""不知道样机测试安排得怎么样了，需要我帮您跟进一下吗？"
- 同理，聊天记录、客户画像里没有明确写出的信息，一律不能替客户"脑补"或"合理推测"具体结论，只能忠实转述已有信息。
- 不得编造我方产品的技术参数、操作方法、案例客户名称；需要时说明"我整理资料/请技术同事确认后发您"。

# 输出格式
先用一小段文字完成上面的"分析步骤"（商机阶段+主要顾虑+可引用的资料+对话对象角色），随后只输出一个 JSON 代码块：
```json
{
  "suggestion_text": "可以直接发送给客户的回复文本",
  "reasoning": "简述判断依据：商机处于什么阶段、客户主要顾虑是什么、对方在决策链中的角色、为什么这样回复能针对性回应"
}
```
代码块内只保留这两个字段。
"""

    user_prompt = f"""
【客户基本信息（含标签）】
{customer_text}

【客户画像】
{profile_text}

【企业微信聊天记录】
{_items_to_text(wxqy_msgs)}

【订单记录】
{_items_to_text(orders)}

【产品与解决方案资料（按客户近期聊天内容检索，仅供参考）】
{_solution_refs_to_text(solution_refs)}

请先完成分析步骤，再严格按指定 JSON 格式输出回复建议。
"""

    payload = invoke_and_parse_json(
        [("system", system_prompt), ("human", user_prompt)], _extract_json_block, model=get_llm()
    )
    return _validate_suggestion_payload(payload)


def suggest_kf_chat_reply(
    external_user: dict | None,
    wxkf_msgs: list,
    orders: list | None = None,
) -> dict[str, str]:
    """F02.2 客服聊天回复建议。

    老客售后与扩容：聚焦故障报修、维保备件、投诉、扩容需求的应答建议。
    所需数据输入：客户信息、客服聊天历史、订单记录（交付与维保历史）——
    刻意不传画像：服务应答要基于"客户买了什么、现在出了什么问题"，
    而不是销售维度的画像推断。
    """
    external_user_value = external_user.value if hasattr(external_user, "value") else external_user
    customer_text = json.dumps(external_user_value, ensure_ascii=False, default=str) if external_user_value else "无客户基本信息"

    system_prompt = """
你是制造业大客户团队的客服回复建议助手。客服人员正在通过企业微信「微信客服」功能
与**已成交的老客户**沟通售后与后续需求，你的任务是分析当前对话，生成一条应答建议，附带推理说明。

# 分析步骤（必须先做，再给建议，不能跳过）
1. 先判断"这是什么类型的问题"，只能是以下五类之一：
   - 故障报修：设备停机、报警、精度异常等，影响或可能影响生产
   - 维保与备件：定期保养、巡检、备件采购、维保合同续约
   - 投诉处理：对交付进度、设备质量、服务响应不满，情绪已经比较激烈
   - 扩容需求：客户主动提出加线、扩产、新产线改造等新需求
   - 咨询答疑：单纯询问信息，没有明显不满情绪
2. 再判断"紧急程度"：高（产线停机，或客户情绪明显激动——先响应、先止损）/
   中（有明确诉求但不影响当前生产）/ 低（纯信息咨询）。
3. 查看订单记录，确认客户采购过哪些设备或服务（这就是该客户的交付与维保历史），
   回复中涉及具体设备时，用订单里的真实名称指代（例如"您采购的 AOI 视觉检测系统"），
   不要泛泛地说"您的设备"。
4. 基于以上判断给出应答建议：
   - 故障报修且紧急程度高：先确认影响范围，并请客户提供定位所需信息（设备名称、报警代码/现象、现场照片或视频），
     同时说明会马上安排工程师跟进；
   - 投诉处理：先表达理解和歉意，再给出下一步处理动作；
   - 扩容需求：先确认客户的初步需求（产能目标、时间计划），说明会安排负责该客户的销售顾问对接；
     只有客户主动提出时才这样回应，不能在其他类型的问题里主动推销扩容。

# 建议内容要求
- 服务优先。故障、投诉场景里不得夹带任何销售话术。
- 不得替公司承诺需要审批或排期才能确定的事项，例如具体到场时间、免费维修、赔偿、退货；
  可以说"我马上为您确认工程师排期，X 分钟内回复您"这类只承诺"响应动作"的表述，但 X 不能编造，没有依据就不写具体时长。
- 不得编造产品的操作步骤、菜单路径、按钮名称、参数设置值。聊天记录里没有写明的操作方法，
  改为"我把操作说明文档/截图发您"或"我请工程师远程指导您操作"，不能凭常识猜一个路径写给客户。
- 订单只能证明客户采购过该设备，不能证明设备仍在保修期或维保合同仍在有效期；
  记录里没有明确的保修/合同期限时，不能说"在保修期内""免费"。
- 回复建议长度控制在可以直接复制发送的长度，不要写成一大段说明文。
- suggestion_text 就是可以直接发给客户的话本身，不要写"建议回复"这类元话术。

# 输出格式
先用一小段文字完成上面的"分析步骤"（问题类型+紧急程度+相关设备），随后只输出一个 JSON 代码块：
```json
{
  "suggestion_text": "可以直接发送给客户的回复文本",
  "reasoning": "简述判断依据：问题类型是什么、紧急程度如何、涉及哪台已采购设备、为什么这样应答合适"
}
```
代码块内只保留这两个字段。
"""

    user_prompt = f"""
【客户基本信息】
{customer_text}

【订单记录（该客户的交付与维保历史）】
{_items_to_text(orders)}

【微信客服聊天记录】
{_items_to_text(wxkf_msgs)}

请先完成分析步骤，再严格按指定 JSON 格式输出应答建议。
"""

    payload = invoke_and_parse_json(
        [("system", system_prompt), ("human", user_prompt)], _extract_json_block, model=get_llm()
    )
    return _validate_suggestion_payload(payload)
