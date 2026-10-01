"""F04 客户标签智能推荐的 LLM 调用与结果校验。

标签推荐与画像生成的区别：这里不是推断一个连续字段值和置信度，而是在
系统已经定义好的标签体系中，判断哪些标签应新增、哪些已有标签应移除。
LLM 负责阅读业务证据；标签 ID 的合法性、方向边界与重复项由本文件在
代码层再次校验，不能只依赖 prompt 自觉遵守。
"""

import json
import os
import re
import sys
from typing import Any

from src.llm.llm_setting import get_llm
from src.llm.json_retry import invoke_and_parse_json

# 仅开发环境诊断用：设置环境变量 KAM_DEBUG_LLM_RAW=1 后，会把这次LLM的
# 原始回复原文打到 stderr（不含 API Key，response.content 只是补全文本，
# 密钥走在 HTTP 请求头里，不会出现在这里）。默认不设置就完全不打印，
# 不影响正常运行；排查完可以直接不设这个环境变量，不需要改代码。
# 用 print+stderr 而不是 logging 模块，是因为这个项目目前没有配置过
# 日志系统，logging.debug() 在默认日志级别下大概率打印不出来，容易踩
# "看起来没生效"的坑，直接 print 更保险、所见即所得。
_DEBUG_LLM_RAW = os.environ.get("KAM_DEBUG_LLM_RAW") == "1"


# 单选标签组：同一客户同组只应有一个标签（口径见 设计文档/12-数据字典.md 第四节）。
# Store 的标签结构里没有"组内互斥"字段，这里在代码层兜底，不只依赖 prompt 自觉遵守。
SINGLE_SELECT_GROUP_IDS = {"industry", "company_type", "intent", "value"}


def _enforce_single_select(
    add: list[dict[str, str]],
    remove: list[dict[str, str]],
    current_ids: set[str],
    catalog: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """单选组兜底规则。

    1. 每个单选组最多保留 1 个新增建议（按模型给出的顺序取第一个）；
    2. 新增了单选组标签、而客户身上还有同组旧标签且模型没建议移除时，
       自动补一条移除建议——最终仍由顾问人工确认，这里只保证候选项不自相矛盾。
    """
    kept_add: list[dict[str, str]] = []
    used_groups: set[str] = set()
    for item in add:
        group_id = catalog[item["tag_id"]].get("group_id", "")
        if group_id in SINGLE_SELECT_GROUP_IDS:
            if group_id in used_groups:
                continue
            used_groups.add(group_id)
        kept_add.append(item)

    final_remove = list(remove)
    removing = {item["tag_id"] for item in remove}
    for item in kept_add:
        group_id = catalog[item["tag_id"]].get("group_id", "")
        if group_id not in SINGLE_SELECT_GROUP_IDS:
            continue
        for old_id in sorted(current_ids):
            if old_id in removing or catalog[old_id].get("group_id") != group_id:
                continue
            final_remove.append(
                {
                    "tag_id": old_id,
                    "tag_name": catalog[old_id]["tag_name"],
                    "reason": f"与建议新增的同组单选标签「{item['tag_name']}」互斥",
                }
            )
            removing.add(old_id)
    return kept_add, final_remove


def _extract_json_block(text: str) -> dict:
    """从 LLM 回复中取出 JSON 代码块；没有代码块时按整段 JSON 解析。"""
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    json_text = match.group(1) if match else text
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError("DeepSeek 返回的不是合法 JSON") from error


def _items_to_text(items: list) -> str:
    """把 Store Item 列表或普通 dict 列表转换为可放入 prompt 的 JSON。"""
    if not items:
        return "无"
    values = [item.value if hasattr(item, "value") else item for item in items]
    return json.dumps(values, ensure_ascii=False, default=str)


def _normalize_tag_catalog(tag_catalog: list) -> dict[str, dict[str, Any]]:
    """将不同来源的标签目录统一成 ``{tag_id: tag_dict}``。

    LangGraph Store 的 tag_id 存在 Item.key，不在 Item.value；主图接线时必须
    保留它。此处也兼容 API 层 list_tags() 已补上的 ``_key``，方便单测和
    后续调用方复用。
    """
    normalized: dict[str, dict[str, Any]] = {}
    for item in tag_catalog:
        if hasattr(item, "key") and hasattr(item, "value"):
            tag_id, raw = item.key, item.value
        elif isinstance(item, dict):
            tag_id = item.get("tag_id") or item.get("_key")
            raw = item
        else:
            continue

        if not isinstance(tag_id, str) or not tag_id.strip() or not isinstance(raw, dict):
            continue
        if raw.get("deleted") is True:
            continue

        tag_name = raw.get("tag_name")
        if not isinstance(tag_name, str) or not tag_name.strip():
            continue

        normalized[tag_id] = {
            "tag_id": tag_id,
            "tag_name": tag_name.strip(),
            "group_id": raw.get("group_id", ""),
            "group_name": raw.get("group_name", ""),
            "strategy_id": raw.get("strategy_id"),
        }
    return normalized


def _normalize_recommendations(
    raw_recommendations: Any,
    allowed_ids: set[str],
    catalog: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """白名单过滤 LLM 输出，并由系统目录回填可信的标签名称。"""
    if not isinstance(raw_recommendations, list):
        return []

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_recommendations:
        if not isinstance(item, dict):
            continue
        tag_id = item.get("tag_id")
        reason = item.get("reason")
        if (
            not isinstance(tag_id, str)
            or tag_id not in allowed_ids
            or tag_id in seen
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            continue
        seen.add(tag_id)
        result.append(
            {
                "tag_id": tag_id,
                "tag_name": catalog[tag_id]["tag_name"],
                "reason": reason.strip(),
            }
        )
    return result


def suggest_tag_recommendations(
    current_tags: list[str],
    tag_catalog: list,
    wxqy_msgs: list,
    wxkf_msgs: list,
    orders: list,
) -> dict[str, list[dict[str, str]]]:
    """根据客户证据，返回 ``add`` / ``remove`` 两组候选标签。

    返回的每条推荐包含 tag_id、tag_name、reason。即使模型返回越界 ID，
    本函数也会在返回前过滤掉，确保后续 interrupt 的候选项合法。
    """
    catalog = _normalize_tag_catalog(tag_catalog)
    current_ids = {tag_id for tag_id in current_tags if tag_id in catalog}
    catalog_text = json.dumps(list(catalog.values()), ensure_ascii=False)

    # 仅开发环境诊断用： 真实联调发现"移除候选一直为空"的现象后
    # 新增。current_ids 是 current_tags 和 catalog 的交集——如果客户身上
    # 预置的标签id和目录里同名标签的真实id对不上（比如预置时手写了占位
    # 符字符串，没有从标签目录里复制真实的长随机id），这里会把它静默丢弃，
    # current_ids 变成空集合，prompt里"客户当前标签"这一节就会是空数组，
    # LLM看不到任何"已有标签"，remove候选自然无从谈起——这和之前怀疑的
    # checkpoint反序列化问题是不同的假设，需要靠这段打印直接验证是不是它。
    if _DEBUG_LLM_RAW:
        dropped_ids = set(current_tags) - current_ids
        print(
            f"[llm_suggest_tag DEBUG] 传入的 current_tags(原始) = {current_tags!r}\n"
            f"[llm_suggest_tag DEBUG] catalog 归一化后共 {len(catalog)} 条标签\n"
            f"[llm_suggest_tag DEBUG] current_ids(与catalog交集后) = {sorted(current_ids)!r}\n"
            f"[llm_suggest_tag DEBUG] 被交集丢弃的id（在current_tags但不在catalog里）= {sorted(dropped_ids)!r}\n",
            file=sys.stderr,
        )

    system_prompt = """
你是制造业大客户销售团队的客户标签推荐助手。你的任务是根据客户的聊天记录和订单，
在系统给定的标签目录中推荐"建议新增"和"建议移除"的客户标签，供顾问最后人工确认。

# 标签判断原则
1. 只可使用标签目录中出现的 tag_id；不能发明标签、改写 ID，不能输出已删除标签。
2. 新增（add）只能从客户当前没有的标签中选择；移除（remove）只能从客户当前已有的标签中选择。
3. 只在证据明确、具体时推荐。聊天中没有提到某个标签相关信息，不是移除它的理由；
   客户近期没有采购，也不能据此移除"老客复购"等基于历史成交的标签。
4. "建议移除"必须存在直接反证或明确状态变化：例如已有"低意向"标签，客户明确说
   "项目立项了，下个月招标"；或已有"暂无需求"，客户主动询问加线方案。
   不要把普通的信息缺失、犹豫、时间过去当作反证。
5. **单选组**：所属行业、企业类型、意向等级、客户价值 这四组，每个客户同组只应有一个标签。
   - 每个单选组最多新增 1 个标签；
   - 新增单选组标签时，如果客户已有同组的其他标签，必须同时在 remove 中建议移除那个旧标签。
6. **多选组要克制**：需求方向、关注重点 两组只在客户**本人明确提出**时推荐。
   - 需求方向：客户提到了具体的项目、设备或采购意向（如"想上一套视觉检测""仓库想做 AGV"）；
     顾问单方面介绍的产品、客户顺口一问，都不算。
   - 关注重点：客户反复强调，或明确作为决策条件的因素（如"交期必须在三月前，否则不考虑"
     "老板只看多久能回本"）；只提过一次且没有表明是决策条件的，不推荐。
7. 标签目录只有名称和分组，按以下口径理解（与团队的业务口径一致）：
   - 意向等级：高意向 = 已立项或有明确采购时间表；中意向 = 有明确需求、正在评估方案；
     低意向 = 泛泛了解、没有具体需求；暂无需求 = 客户明确表示近期不做。
   - 客户价值：战略大客户 = 集团级、多基地或年采购规模很大；重点客户 = 有百万级以上的明确项目；
     普通客户 = 其他；老客复购 = 已有成交订单，并再次提出新需求。
   - 所属行业、企业类型：只在客户明确提到或订单/聊天内容能直接确定时推荐，不按公司名称猜测。
   语义不够明确或证据不足时，宁可不推荐，绝不猜测。
8. 每条 reason 必须说明哪一条具体证据支持结论，尽量包含数据来源和时间；
   不要只写"符合标签定义"或"客户可能需要"。
9. 不需要穷举，更不要把一组里的标签全部勾上。一次建议新增通常不超过 3 个；
   没有可靠建议时，对应数组返回空数组。

# 输出格式
先用极短的"证据梳理"按时间说明你准备推荐/移除的依据，随后只输出一个 JSON 代码块：
```json
{
  "add": [
    {"tag_id": "目录中的真实ID", "tag_name": "目录中的真实名称", "reason": "具体证据"}
  ],
  "remove": [
    {"tag_id": "目录中的真实ID", "tag_name": "目录中的真实名称", "reason": "直接反证"}
  ]
}
```
代码块内只保留 add、remove 及其条目字段；没有候选就使用空数组。
"""

    user_prompt = f"""
【有效标签目录】
{catalog_text}

【客户当前标签 ID】
{json.dumps(sorted(current_ids), ensure_ascii=False)}

【企业微信聊天记录】
{_items_to_text(wxqy_msgs)}

【微信客服聊天记录】
{_items_to_text(wxkf_msgs)}

【订单记录】
{_items_to_text(orders)}

请先做证据梳理，再严格按指定 JSON 格式输出标签建议。
"""

    payload = invoke_and_parse_json(
        [("system", system_prompt), ("human", user_prompt)], _extract_json_block, model=get_llm()
    )
    if not isinstance(payload, dict):
        raise ValueError("DeepSeek 返回的标签建议必须是 JSON 对象")

    raw_add = payload.get("add")
    raw_remove = payload.get("remove")
    normalized_add = _normalize_recommendations(
        raw_add, set(catalog) - current_ids, catalog
    )
    normalized_remove = _normalize_recommendations(
        raw_remove, current_ids, catalog
    )
    normalized_add, normalized_remove = _enforce_single_select(
        normalized_add, normalized_remove, current_ids, catalog
    )

    # 诊断日志：解析后、白名单过滤前后的对比——如果 raw_add/raw_remove 本身
    # 就是空列表，说明是LLM没给候选（大概率是证据判定问题，该去看prompt/
    # 证据措辞）；如果 raw_* 非空但 normalized_* 是空，说明是过滤逻辑把
    # 合法候选误杀了（该去看 _normalize_recommendations 或 tag_id 格式问题）。
    if _DEBUG_LLM_RAW:
        print(
            f"[llm_suggest_tag DEBUG] 解析后 raw_add={raw_add!r}\n"
            f"[llm_suggest_tag DEBUG] 解析后 raw_remove={raw_remove!r}\n"
            f"[llm_suggest_tag DEBUG] 过滤后 normalized_add={normalized_add!r}\n"
            f"[llm_suggest_tag DEBUG] 过滤后 normalized_remove={normalized_remove!r}\n",
            file=sys.stderr,
        )

    return {
        "add": normalized_add,
        "remove": normalized_remove,
    }
