"""按数据字典写入六组制造业客户标签，可重复运行。"""

import os

from dotenv import load_dotenv
from langgraph.store.postgres import PostgresStore

from src.store.store_client import upsert_tags_setting


TAG_GROUPS = {
    "industry": ("所属行业", [
        ("ind_auto_parts", "汽车零部件"), ("ind_3c", "3C 电子"),
        ("ind_new_energy", "新能源（锂电/光伏）"), ("ind_machinery", "工程机械"),
        ("ind_appliance", "家电"), ("ind_food", "食品饮料"),
        ("ind_medical", "医疗器械"),
    ]),
    "company_type": ("企业类型", [
        ("type_state", "国央企"), ("type_listed", "民营上市"),
        ("type_sme", "民营中小"), ("type_joint", "外资/合资"),
    ]),
    "demand": ("需求方向", [
        ("demand_line", "产线自动化改造"), ("demand_robot", "机器人工作站"),
        ("demand_vision", "视觉检测"), ("demand_mes", "MES/数字化"),
        ("demand_warehouse", "仓储物流自动化"), ("demand_service", "设备维保"),
    ]),
    "intent": ("意向等级", [
        ("intent_high", "高意向"), ("intent_medium", "中意向"),
        ("intent_low", "低意向"), ("intent_none", "暂无需求"),
    ]),
    "value": ("客户价值", [
        ("value_strategic", "战略大客户"), ("value_key", "重点客户"),
        ("value_normal", "普通客户"), ("value_repeat", "老客复购"),
    ]),
    "concern": ("关注重点", [
        ("concern_delivery", "关注交付周期"), ("concern_tech", "关注技术方案"),
        ("concern_service", "关注售后响应"), ("concern_roi", "关注投资回报"),
    ]),
}


def main() -> None:
    load_dotenv()
    uri = os.environ.get("KAM_POSTGRES_URL")
    if not uri:
        raise RuntimeError("KAM_POSTGRES_URL not set")
    with PostgresStore.from_conn_string(uri) as store:
        store.setup()
        for group_id, (group_name, tags) in TAG_GROUPS.items():
            for tag_id, tag_name in tags:
                upsert_tags_setting(tag_id, tag_name, False, "demo", group_id, group_name, store=store)
    print(f"Seeded {sum(len(tags) for _, tags in TAG_GROUPS.values())} tags")


if __name__ == "__main__":
    main()
