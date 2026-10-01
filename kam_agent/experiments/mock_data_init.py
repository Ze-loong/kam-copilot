"""写入六名员工和三个画像置信度对照客户。"""

import os
from dotenv import load_dotenv
from langgraph.store.postgres import PostgresStore
from src.store.store_client import (upsert_employee, upsert_external_user,
    upsert_wxqy_msg, upsert_wxkf_msg, upsert_wxxd_order)

EMPLOYEES = [
    ("demo_emp_xiaozhang", "小张", "consultant", "华东"),
    ("demo_emp_xiaolin", "小林", "consultant", "华南"),
    ("demo_emp_xiaozhao", "小赵", "consultant", "华北"),
    ("demo_emp_manager", "李总监", "regional_manager", "华东"),
    ("demo_emp_manager_south", "周总监", "regional_manager", "华南"),
    ("demo_emp_admin", "超级管理员", "super_admin", "全国"),
]

BASE_CUSTOMERS = [
    dict(slug="zhou", name="周工", company="苏州联拓汽车零部件有限公司",
         tags=["ind_auto_parts", "demand_robot", "intent_high"],
         dialogue=[
             "我是联拓设备部周工，我们做车身冲压件，约800人，年产值6亿。",
             "三条焊装线目前半自动，人工点焊返修多，招工也难。",
             "想把焊装节拍从60秒降到45秒，先改一条线。",
             "项目已经立项，预计明年一季度招标，预算300到500万。",
             "设备部提需求，生产副总把技术关，董事长最后拍板。",
             "请带两台六轴机器人和变位机的工作站方案来现场勘查。",
             "我们也接触两家本地集成商，会比较交付周期和案例。",
             "周二上午沟通方便，报价超过400万要重新报批。",
             "技术评审通过了，合同按招标流程走。",
             "焊装线机器人工作站已验收，后续考虑第二条线。",
         ], service=[], orders=["焊装线机器人工作站（2 台六轴机器人 + 变位机）"]),
    dict(slug="chen", name="陈经理", company="东莞锐芯电子有限公司",
         tags=["ind_3c", "intent_low", "demand_vision"],
         dialogue=[
             "我是锐芯采购经理陈经理，电子装配线准备做视觉检测。",
             "九月项目已经立项，先把检测良率提上去。",
             "两条线每天三班，人工复检耗时，AOI 参数要现场验证。",
             "请提供两工位样机的节拍和误判率数据。",
             "样机测试可以先下小订单，正式采购还要技术评审。",
             "我们也在比较另一家视觉集成商的方案。",
             "十一月重新盘了一遍，现在还在做可行性调研，立项要等明年。",
             "现在主要是为了减人，检测良率先放一边。",
             "下周请来厂里看产线，采购流程还得走审批。",
             "报价先按样机规模发我，整线预算还没定。",
         ], service=["AOI 样机试运行的误检数据怎么导出？", "工程师可导出检测日志，我安排远程协助。"],
         orders=["AOI 视觉检测样机测试"]),
    dict(slug="liu", name="刘总", company="合肥鼎泰食品有限公司",
         tags=["ind_food", "intent_low"],
         dialogue=[
             "我是鼎泰的刘总，包装产线想了解自动化设备。",
             "整线交钥匙大概多少钱？先报个区间。",
             "一条线能做到多少箱每小时？",
             "先买一台单机试试也行，具体还没想定。",
             "再给我一份整线报价，我看看。",
             "仓储环节如果一起做，交期会增加多少？",
             "上次说的单机方案也留着，不急着定。",
             "有没有维保费用的说明？先发资料。",
             "价格再核一下，我还要考虑。",
             "年底可能再谈，现在没有采购决定。",
         ], service=["包装单机和整线方案价差多少？", "配置范围不同，顾问会分别报价。", "单机能先试用吗？暂时只是了解。"],
         orders=[]),
]


def write_customer(store, customer: dict, employee_id: str = "demo_emp_xiaozhang") -> None:
    """用固定标识和时间写入客户，重复运行只覆盖原记录。"""
    slug = customer["slug"]
    external_id, union_id = f"demo_ext_{slug}", f"demo_union_{slug}"
    upsert_external_user(external_id, union_id, employee_id, customer["name"],
                         customer["company"], customer["tags"], store=store)
    for index, content in enumerate(customer["dialogue"], 1):
        month = "09" if index <= 4 else "10" if index <= 6 else "11" if index <= 8 else "12"
        stamp = f"2025-{month}-{index + 3:02d} 10:{index:02d}:00"
        advisor_reply = customer.get("alternate_speakers", False) and index % 2 == 0 and index <= 8
        sender = employee_id if advisor_reply else external_id
        receiver = external_id if advisor_reply else employee_id
        upsert_wxqy_msg(employee_id, external_id, f"demo_qy_{slug}_{index:02d}",
                        sender, receiver, content, stamp, stamp[:10].replace("-", ""), store=store)
    for index, content in enumerate(customer["service"], 1):
        stamp = f"2025-12-{index + 5:02d} 14:{index:02d}:00"
        upsert_wxkf_msg(external_id, f"demo_kf_{slug}_{index:02d}", content,
                        stamp, "customer" if index % 2 else "staff",
                        stamp[:10].replace("-", ""), store=store)
    for index, product in enumerate(customer["orders"], 1):
        stamp = f"2025-11-{index + 18:02d} 11:00:00"
        upsert_wxxd_order(union_id, f"demo_order_{slug}_{index:02d}", [product],
                          stamp, stamp[:10].replace("-", ""), store=store)


def main() -> None:
    load_dotenv()
    uri = os.environ.get("KAM_POSTGRES_URL")
    if not uri:
        raise RuntimeError("KAM_POSTGRES_URL not set")
    with PostgresStore.from_conn_string(uri) as store:
        store.setup()
        for employee_id, name, role, region in EMPLOYEES:
            upsert_employee(employee_id, name, role, region, "demo123", store=store)
        for customer in BASE_CUSTOMERS:
            write_customer(store, customer)
    print("Seeded 6 employees and 3 baseline customers")


if __name__ == "__main__":
    main()
