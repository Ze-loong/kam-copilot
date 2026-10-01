import json
import re

from src.llm.llm_setting import get_llm
from src.llm.json_retry import invoke_and_parse_json
from src.models.kam_models import ProfileFieldItem

ALLOWED_PROFILE_FIELDS = {
    "company_name",
    "project_stage",
    "company_scale",
    "production_status",
    "budget_range",
    "tech_goal",
    "pain_points",
    "decision_makers",
    "reply_time",
    "price_sensitivity",
    "decision_style",
    "competitor_info",
}

def _extract_json_block(text: str) -> dict:
    """
    从 LLM 回答里取出最终 JSON。

    现在的 prompt 要求 LLM 先写一段"时间线梳理"文字，再用 ```json 代码块
    输出最终结果——这是为了让它在给结论前先显式地把证据按时间顺序过一遍，
    减少"看一眼就下结论、漏掉矛盾"的情况。也因此不能再像以前一样直接
    json.loads(response.content)，要先把代码块从整段回答里切出来。
    """
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    json_text = match.group(1) if match else text

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError("DeepSeek 返回的不是合法 JSON") from error


def _items_to_text(items: list) -> str:
    """把 Store 查询返回的 Item 列表，转成可放进 prompt 的 JSON 文本。"""
    if not items:
        return "无"

    values = [
        item.value if hasattr(item, "value") else item
        for item in items
    ]
    return json.dumps(values, ensure_ascii=False, default=str)

def suggest_profile_draft(
    wxqy_msgs: list,
    wxkf_msgs: list,
    orders: list,
) -> dict[str, ProfileFieldItem]:
    """
    根据聊天记录和订单数据，生成客户画像字段草稿。

    返回格式：
    {
        "project_stage": {
            "value": "已立项，预计明年一季度招标",
            "confidence": 8,
            "source": "2025-10-12 企微聊天：客户说立项批下来了",
            "status": "draft",
        }
    }
    """

    wxqy_text = _items_to_text(wxqy_msgs)
    wxkf_text = _items_to_text(wxkf_msgs)
    orders_text = _items_to_text(orders)

    system_prompt = """
你是制造业大客户销售团队的客户画像分析助手。
客户是制造企业的对接人（设备部经理、采购经理、生产负责人、老板等），
销售顾问通过企业微信与其沟通产线技改方案与售后维保。
你的任务是：只根据提供的聊天记录和订单记录，推断客户画像。

允许使用的字段名只有（括号内为含义）：
- company_name（所属企业）、company_scale（企业规模：人数、产值、产线数量等）、
  production_status（主营产品与产线现状）
- project_stage（技改项目阶段）、pain_points（核心痛点）、tech_goal（技改目标）、
  budget_range（预算区间）
- decision_makers（决策链：谁提需求、谁把技术关、谁最终拍板）、
  decision_style（决策风格）、price_sensitivity（价格敏感度）
- reply_time（活跃沟通时段）、competitor_info（竞品接触情况）

# 分析步骤（必须先做，再输出结论）

在给出最终 JSON 之前，你必须先在回答里写一段"时间线梳理"：
对每一个你打算输出的字段，把和它相关的证据按时间先后列出来
（格式类似："2025-09-05 客户说XXX" → "2025-11-08 客户说YYY"），
再基于这份时间线判断这个字段的证据是否一致、是否存在冲突。
写完时间线之后，再输出最终的 JSON 结果。

# 置信度判断规则

1. 只输出有证据支持的字段；没有证据就不要输出，绝对不能编造。
2. confidence 必须是 0 到 10 的整数。
3. source 必须写出证据来自哪一类数据以及简短依据。
4. status 固定返回 "draft"，低置信度的状态由系统代码处理。
5. confidence 衡量你对"该字段当前取值"的把握，不等于客户态度是否积极——
   客户表现得越犹豫/信息量越大，不代表你对结论的把握越高，这是两件独立的事。
6. 同一字段若存在无法解释的相互冲突证据，或证据只是不明确的试探、转述、未落地意向，
   不要自行选择一个看似合理的结论；应输出待核实假设，
   confidence 必须为 0-3，并在 source 中写明不确定或冲突的具体原因。
7. 区分"信息补充/进展"与"同一事实的直接否定或反转"：
   - 信息补充/进展：新证据是在原有基础上补全细节，不否定旧证据
     （例如上次只说"有预算"，这次说出"300 到 500 万"）。
   - 直接否定/反转：新证据在字面上和旧证据相互矛盾或对立
     （例如先说"预算已经批了"后说"预算还没报"；先说"主要为了提良率"后说"主要为了减人"）。
   直接否定/反转类必须按第6条处理，输出低置信度，
   不能仅凭"证据时间更新"就直接采信较新一条、当作没有矛盾。
   **对 project_stage（技改项目阶段）要格外谨慎**：
   - 技改项目通常按 调研 → 方案 → 立项 → 招标 → 签约 → 实施/验收 推进，
     但也会停滞或倒退（预算被砍、项目搁置、负责人变动）。
   - 两条证据阶段不同时，只有聊天中出现**明确的推进事件**
     （如"立项批下来了""招标文件已经挂网""合同签了""设备验收了"）才能采信较新的阶段；
   - 没有推进事件、只是前后说法不同 → 按矛盾处理，confidence 0-3，source 写明两条冲突证据；
   - 阶段**倒退**（较新证据的阶段早于较旧证据，例如先说"已立项"后说"还在做可行性调研"）
     → 一律 confidence 0-3，source 注明"阶段倒退，疑似项目搁置或信息有误"。
   - 你无法从聊天记录判断项目的真实进度，不能自行假设"时间过去这么久，应该是正常推进了"。
   **对 tech_goal（技改目标）要格外谨慎**：目标类表述没有客观的自然演进规律，
   只要出现方向性变化（比如从"提升检测良率"变成"主要为了减少人工"），
   默认按矛盾处理给低置信度，除非客户在原文中明确解释了转变原因
   （比如"老板看了人工成本报表，重点改成减人了"）。
   **对 budget_range（预算区间）**：只采信客户明确说出的金额或区间；
   我方报价、客户询问"大概多少钱"都不是客户的预算。
8. 多次一致、明确的证据应给中高置信度——但"一致"指的是证据内容本身传达的是
   同一个明确结论，而不是"多次表达同一种不确定/犹豫态度"。
   例如客户多次明确说"超过 400 万就要重新走集团审批"并据此要求拆分报价，
   可支持"价格敏感度较高"给中高置信度；
   但如果反复出现的是"再评估评估""等领导看看""先放一放"这类未落地的犹豫表述，
   即使出现次数很多，也说明客户的真实倾向本身无法判断，属于第6条的
   "不明确的试探"，必须给低置信度，不能因为"证据数量多"就误判为"证据一致"。
9. **重要区分：客户"询问"某个信息，不等于客户对该信息给出了明确"结论"**。
   price_sensitivity（价格敏感度）、decision_style（决策风格）这类需要
   总结客户"行为倾向/态度模式"的字段尤其容易被这个陷阱误导：
   - 客户反复问价格本身只能说明"客户关心价格"，不能直接推出"价格敏感度高/低"——
     只有当客户对价格给出了明确反应（比如听到报价后明确说"超预算了""这个价可以接受"、
     据此砍掉部分配置、或直接放弃）才算有效证据；只是"问了但没表态就转移话题/
     说要再想想"，等于没有证据，必须给低置信度。
   - 客户在"整线交钥匙/先买单机试用""公开招标/直接议价"等方案之间反复更改说法、
     且没有说明最终选择或改变原因，这是"决策不稳定/摇摆"的直接证据，
     不能把其中任何一次表态当作"最终决策风格"来下结论，也必须给低置信度并在
     source 里如实写"客户表态反复，未形成稳定结论"。
10. **证据引用范围**：
   - 订单记录只能证明"客户采购了什么"，不能推出设备的执行状态（是否已到厂、已验收、运行是否良好）。
   - company_scale 只采信客户明确说出的人数、产值、产线数，不能从订单金额或采购规模反推企业规模。
   - reply_time 需要多条消息的时间呈现出稳定规律才能输出；消息零散、看不出规律时不要输出该字段。
   - decision_makers 只写聊天中明确提到的角色与分工；没提到最终拍板人就写"最终决策人未明确"，不能猜测。
11. 只返回合法 JSON 结构的最终结果，用 ```json 代码块包裹，
   代码块前面是你的时间线梳理文字，代码块内只放 JSON，不要在代码块内写解释。
"""

    user_prompt = f"""
【企业微信聊天记录（已按 msg_time 字段标注时间，请注意查看每条记录的时间先后）】
{wxqy_text}

【微信客服聊天记录】
{wxkf_text}

【订单记录】
{orders_text}

请先按系统提示的步骤写时间线梳理，再用 ```json 代码块输出最终结果，
代码块内格式如下：

```json
{{
  "project_stage": {{
    "value": "已立项，预计明年一季度招标",
    "confidence": 8,
    "source": "2025-10-12 企业微信聊天：客户说立项批下来了，招标文件在准备",
    "status": "draft"
  }}
}}
```
"""
    draft = invoke_and_parse_json(
        [
            ("system", system_prompt),
            ("human", user_prompt),
        ], _extract_json_block, model=get_llm(),
    )
    if not isinstance(draft, dict):
        raise ValueError("DeepSeek 返回的画像草稿必须是 JSON 对象")

    normalized_draft: dict[str, ProfileFieldItem] = {}

    for field_name, field_data in draft.items():
        if field_name not in ALLOWED_PROFILE_FIELDS:
            continue

        if not isinstance(field_data, dict):
            continue

        value = field_data.get("value")
        confidence = field_data.get("confidence")
        source = field_data.get("source")

        if not isinstance(value, str) or not value.strip():
            continue

        if not isinstance(confidence, int) or not 0 <= confidence <= 10:
            continue

        if not isinstance(source, str) or not source.strip():
            continue

        normalized_draft[field_name] = {
            "value": value.strip(),
            "confidence": confidence,
            "source": source.strip(),
            "status": "draft",
        }

    return normalized_draft
