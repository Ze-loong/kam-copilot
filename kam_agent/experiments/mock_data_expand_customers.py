"""十一位手写演示客户，覆盖 F02 四阶段和 F03 故障、维保、投诉、答疑、扩容。"""

import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
from langgraph.store.postgres import PostgresStore
from src.store.store_client import upsert_external_user, upsert_wxqy_msg, upsert_wxkf_msg, upsert_wxxd_order


# 每人的企微对话均从客户开始，随后顾问与客户交替发言；全部为虚构演示数据。
CUSTOMERS = [
    dict(slug="zhao", name="赵工", company="杭州精晟结构件有限公司", employee="demo_emp_xiaozhang", tags=["ind_3c", "demand_vision"], start="2025-09-08 09:12", step=17, dialogue=[
        "精密结构件外观检现在靠人工抽检，两家视觉方案都在评估。", "赵工，您负责技术验收吗？我先了解检测工位。",
        "对，设备部由我把技术关。你们误检率怎么验证？", "可以按贵司缺陷样本做现场测试，记录误检与漏检口径。",
        "我们 MES 要接检测结果，接口协议能给我看看吗？", "我整理接口说明，具体字段与信息化同事一起核对。",
        "另一家也承诺能接 MES，我需要同类精密件案例比较。", "我会提供可公开的同类案例范围和测试方法供您评审。",
        "先别谈采购，案例和接口资料齐了再开技术评审会。"], service=[], orders=[]),
    dict(slug="wang", name="王主任", company="南京迈科医疗装配有限公司", employee="demo_emp_xiaozhang", tags=[], start="2025-09-17 14:06", step=23, dialogue=[
        "装配线视觉检测方案，我们技术组已经签字认可。", "王主任，下一步集团审批还缺哪些材料？",
        "技术这边没问题了，现在是钱的事。", "明白，是需要投资额拆分还是收益测算口径？",
        "老板要看回本周期，我得拿投资回报测算材料去报集团预算。", "我可以整理设备投入、人工复检工时和维护费用的测算表。",
        "收益部分别直接填数字，停线损失要由我们财务确认。", "好的，我标出待贵司确认的数据来源，不代替财务下结论。",
        "周五预算预审前把测算模板给我，采购流程还没启动。"], service=[], orders=[]),
    dict(slug="feng", name="冯总", company="苏州朗居家电制造有限公司", employee="demo_emp_xiaozhang", tags=["ind_appliance", "intent_high"], start="2025-10-09 20:18", step=11, dialogue=[
        "注塑到总装这段改造，合同版本发来我再看一遍。", "冯总，我方正式报价 268 万，交期 14 周，报价单已发您邮箱。",
        "价格我看到了，付款 3-6-1 能不能改成 2-6-2？", "我记录您的付款建议，需与商务和财务核对后回复。",
        "尾款要和最终验收挂钩，验收标准写进合同附件。", "我们把节拍、连续运行和故障处理的验收口径列成附件草案。",
        "连续运行从哪天算，双方签字节点也要写清。", "我请项目经理对齐起算条件，形成可逐条确认的文本。",
        "这些条款定了我就安排法务审，不要另加口头承诺。"], service=[], orders=[]),
    dict(slug="sun", name="孙经理", company="宁波骏驰汽车零部件有限公司", employee="demo_emp_xiaozhang", tags=["ind_auto_parts", "intent_low", "demand_robot"], start="2025-10-21 08:34", step=29, dialogue=[
        "今年两套焊装工作站的巡检记录发我归档。", "孙经理，我核对维保合同和已完成的巡检记录后发您。",
        "2 号工作站最近夹具动作偶尔变慢，先记在记录里。", "收到，我让工程师看日志，避免凭现象直接判断故障。",
        "明年新增一条焊装线的项目已立项，方案评估会放在下个月。", "我记录扩线计划，当前先保证这条线稳定。",
        "维保合同里两次巡检别和临时故障处理混在一起算。", "会分开核对合同条款和本次服务记录。",
        "报警截图我让班长留着，后面需要可以发你们。"], service=[
        "急！2 号工作站六轴机器人报 SRVO-050 停机，整条焊装线停了，今天还有交付压力。",
        "已收到。请先确认现场人员安全，并发报警屏照片、发生时间和复位尝试记录，我们立即协调工程师判断。",
        "照片已发，复位后又报同一代码，别让我一直等消息。"], orders=["焊装线机器人工作站（2 台六轴机器人 + 变位机）", "年度设备维保合同（含 2 次巡检）"]),
    dict(slug="song", name="宋工", company="常州锐能电池系统有限公司", employee="demo_emp_xiaozhang", tags=[], start="2025-11-04 16:42", step=19, dialogue=[
        "PACK 仓库一期 AGV 路线改过一次，旧地图还留着吗？", "宋工，我核对调度系统的地图版本后给您导出记录。",
        "夜班高峰两台车转弯有抖动，我们先观察轮子。", "麻烦记录车号和里程，便于区分机械磨损与路径设置。",
        "备件采购要走内部物料编码，型号别写错。", "我会按设备清单核对驱动轮规格，再给采购用的名称。",
        "合同明年到期，续签流程我们得提前走。", "我整理现有维保合同的期限与服务项，交顾问对接续约。"], service=[
        "AGV-03 和 AGV-07 驱动轮磨损明显，请给两套备件报价。", "收到，我先核实这两台车的轮组规格和数量，再请备件同事出报价。",
        "另外维保合同明年到期，怎么续？别把备件价算进续约里。"], orders=["仓储 AGV 调度系统一期（8 台搬运 AGV + 调度服务器）"]),
    dict(slug="han", name="韩经理", company="深圳安拓电子制造有限公司", employee="demo_emp_xiaolin", tags=["ind_3c", "intent_low", "demand_vision"], start="2025-11-13 11:03", step=31, dialogue=[
        "两工位 AOI 上次调参后，误检报表我留档了。", "韩经理，方便把同批次样本编号也发我吗？",
        "采购问我第一条 SMT 线的问题是否彻底处理。", "目前还要看连续批次数据，我会如实更新处理记录。",
        "第二条 SMT 线的 AOI 已列入明年采购预算，但得先把这条线做好。", "理解，先处理当前误检，再由顾问单独沟通第二条线。",
        "上次报修工程师到现场晚了两天，这件事我还记着。", "这次我会把响应过程和待核实节点完整记录并跟进。",
        "请给我一个能追踪的工单编号，不要只在群里说。"], service=[
        "误检问题又复发了！上次报修工程师晚到两天，产线都被拖着，我要向采购反映。",
        "非常抱歉影响生产。我先登记复发批次、误检图片和当前工单状态，并核对上次处理记录。",
        "这次别再只说会跟进，给我明确的下一步处理人。"], orders=["AOI 视觉检测系统（2 工位，SMT 一线）", "年度设备维保合同（含 2 次巡检）"]),
    dict(slug="lin", name="林总", company="佛山清泉饮品有限公司", employee="demo_emp_xiaolin", tags=[], start="2025-09-26 18:27", step=37, dialogue=[
        "刚加上微信，了解到你们做灌装线自动化？", "林总您好，我们可以先了解一下贵司目前的灌装流程。",
        "现在还没定要改哪段，想先看看同行怎么做。", "可以先分享可公开的食品饮料案例，您关心产能还是现场管理？",
        "我也说不准，能不能先去你们那边参观？", "我核实可参观的演示现场和接待安排后再答复您。",
        "我们这边是小厂，最后我拍板，但车间主管得一起看看。", "明白，参观前可以先列出您和车间主管想重点了解的问题。",
        "先发案例介绍吧，具体方案等看完再说。"], service=[], orders=[]),
    dict(slug="he", name="何经理", company="佛山华宸家电集团有限公司", employee="demo_emp_xiaolin", tags=["ind_appliance", "concern_roi"], start="2025-12-02 09:47", step=13, dialogue=[
        "采购要比较整线交钥匙与分段改造，两种方案请分开列。", "何经理，您希望拆到设备、集成和现场实施三级吗？",
        "至少要看到每段工位、控制柜和软件授权的报价明细。", "我会请方案团队按同一范围口径拆分，便于横向比较。",
        "竞品报价比你们低 15%，我得向评标组解释差异。", "请把对方包含的设备和验收范围发来，我们再核对差项。",
        "国企采购要留审计依据，不能只给一个总价。", "明白，报价明细与假设条件会同时标注在文件里。",
        "先提交两套可比清单，暂时不要替我选方案。"], service=[], orders=[]),
    dict(slug="yang", name="杨工", company="广州诺衡医疗科技有限公司", employee="demo_emp_xiaolin", tags=[], start="2025-12-11 13:16", step=21, dialogue=[
        "一期 MES 的报工数据已经跑起来，班组长反馈扫码还算顺手。", "杨工，设备联网的数据点位有需要补充的吗？",
        "另一车间的设备协议不同，二期得先盘接口。", "可以先整理设备清单和协议版本，避免直接假定兼容。",
        "一期报表导出权限我想再给质量部开一个账号。", "我请管理员核对角色权限，按贵司审批流程处理。",
        "二期现在只是想法，别把它算成已批准项目。", "收到，先记录需求，等您确认范围再安排顾问对接。"], service=[
        "一期 MES 的生产日报从哪里导出 Excel？我在报表页没找到按钮。", "请先确认当前账号的报表权限和所选日期；我发您操作路径截图。",
        "我在个人账号找到了，质量部账号还看不到。请把报表导出操作路径再发一遍；顺便问下，二期想把另一车间设备也联网，谁来对接评估？"], orders=["产线 MES 一期（生产报工 + 设备联网，1 个车间）"]),
    dict(slug="wu", name="吴工", company="徐州重岳工程机械有限公司", employee="demo_emp_xiaozhao", tags=["ind_machinery", "demand_robot"], start="2025-10-30 07:21", step=43, irregular=True, dialogue=[
        "老厂房焊装线那套机器人工作站，控制柜型号资料还有吗？", "吴工，我从交付档案核对型号和软件版本后发您。",
        "新厂房明年要加两条焊接线，设备选型还没定。", "新线若要统一管理，先要核对现有控制系统接口。",
        "总部信息化部也会参与，别只和设备部谈。", "了解，后续接口评估会邀请信息化部一起确认。",
        "维保合同的巡检记录顺便给我一份。", "我核对合同对应的巡检记录，再发您归档。",
        "扩建预算还没批，眼下先确认技术可行性。"], service=[
        "新厂房明年增加两条焊接线，旧焊装线机器人工作站的控制系统能统一管理新线吗？",
        "需要核对旧系统版本、新线设备接口和控制网络架构，我先登记评估需求。",
        "可以，让你们顾问和信息化部约一次技术会，别先报确定能接。"], orders=["焊装线机器人工作站（2 台六轴机器人 + 变位机）", "年度设备维保合同（含 2 次巡检）"]),
    dict(slug="zheng", name="郑经理", company="石家庄北辰汽车部件有限公司", employee="demo_emp_xiaozhao", tags=[], start="2025-12-16 15:32", step=9, dialogue=[
        "焊接自动化招标文件今天挂网了，你们看到了吗？", "郑经理，我会让投标同事按公开文件核对要求。",
        "资质材料、近三年业绩证明具体要准备哪些？", "以招标文件清单为准，我整理现有材料逐项核对缺项。",
        "截标时间很紧，别等到最后一天才问格式。", "我今天把格式和盖章要求发给法务及投标团队确认。",
        "评标有同类项目经验项，案例证明不能只放宣传页。", "明白，我们核实可出具的合同或验收证明范围。",
        "请在截止前把疑问一次性列给我，别替招标方解释规则。"], service=[], orders=[]),
]


def write_story(store, customer: dict) -> None:
    """按各客户的时间节奏写入企微、客服和既有订单。"""
    slug = customer["slug"]
    external_id, union_id = f"demo_ext_{slug}", f"demo_union_{slug}"
    employee = customer["employee"]
    upsert_external_user(external_id, union_id, employee, customer["name"], customer["company"], customer["tags"], store=store)
    start = datetime.strptime(customer["start"], "%Y-%m-%d %H:%M")
    for index, content in enumerate(customer["dialogue"], 1):
        elapsed = ((index - 1) * customer["step"] if not customer.get("irregular")
                   else [0, 18, 73, 96, 321, 349, 1480, 1512, 2940][index - 1])
        stamp = (start + timedelta(minutes=elapsed)).strftime("%Y-%m-%d %H:%M:%S")
        sender, receiver = (external_id, employee) if index % 2 else (employee, external_id)
        upsert_wxqy_msg(employee, external_id, f"demo_qy_{slug}_{index:02d}", sender, receiver, content, stamp, stamp[:10].replace("-", ""), store=store)
    for index, content in enumerate(customer["service"], 1):
        stamp = (start + timedelta(days=12, minutes=index * customer["step"])).strftime("%Y-%m-%d %H:%M:%S")
        upsert_wxkf_msg(external_id, f"demo_kf_{slug}_{index:02d}", content, stamp, "customer" if index % 2 else "staff", stamp[:10].replace("-", ""), store=store)
    for index, product in enumerate(customer["orders"], 1):
        stamp = (start - timedelta(days=25 - index)).strftime("%Y-%m-%d %H:%M:%S")
        upsert_wxxd_order(union_id, f"demo_order_{slug}_{index:02d}", [product], stamp, stamp[:10].replace("-", ""), store=store)


def main() -> None:
    """在标签及基础客户灌入后写入十一位扩充客户。"""
    load_dotenv()
    uri = os.environ.get("KAM_POSTGRES_URL")
    if not uri:
        raise RuntimeError("KAM_POSTGRES_URL not set")
    with PostgresStore.from_conn_string(uri) as store:
        store.setup()
        for customer in CUSTOMERS:
            write_story(store, customer)
    print("Seeded 11 hand-written customer stories")


if __name__ == "__main__":
    main()
