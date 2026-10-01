"""
KamStoreAPI 真实联调探针（不是 unittest，是直接跑的验证脚本，
与 kam_agent/experiments/ 下几个 probe_*.py 同风格）。

前置条件：
  1. kam-postgres 容器已启动
  2. kam_agent 服务已启动（uv run uvicorn src.webapp.webapp:app --reload），
     且 KAM_POSTGRES_URL 已设置好
  3. 本文件运行前设置 KAM_AGENT_BASE_URL（默认 http://127.0.0.1:8000）

运行方式：
  uv run python -m store_client.kam_client_probe

验证内容：
  - upsert_employee → get_employee → list_employees 闭环（employee 命名空间）
  - upsert_tag → get_tag（tags_setting 命名空间）
  - get_customer_detail 聚合查询不报错（即使客户不存在，也应该拿到
    external_user=None / profile=None / orders=[] / tags=[] 的干净结果，
    不应该抛异常——这是聚合查询对"数据不存在"这种正常业务分支的容错要求）

探针数据统一 probe_ 前缀，与既有 probe_/demo_ 命名惯例保持一致，运行后暂留
数据库不清理（跟 Store API 验证脚本 的既有做法一致）。
"""

import os

from dotenv import load_dotenv

from store_client.kam_store_api import create_kam_store_api

load_dotenv()


def main():
    base_url = os.getenv("KAM_AGENT_BASE_URL", "http://127.0.0.1:8000")
    api = create_kam_store_api(base_url)

    print(f"[连接] KAM_AGENT_BASE_URL = {base_url}")

    # ---- employee 闭环 ----
    api.upsert_employee(
        user_id="probe_kam_client_emp001",
        name="探针员工",
        role="consultant",
        region="华东",
    )
    got = api.get_employee("probe_kam_client_emp001")
    assert got is not None, "get_employee 未命中刚写入的数据"
    assert got["name"] == "探针员工", f"字段值不符：{got}"
    print("[PASS] employee upsert → get 闭环，中文字段回读正确")

    all_employees = api.list_employees()
    assert any(e["user_id"] == "probe_kam_client_emp001" for e in all_employees), \
        "list_employees 未搜到刚写入的探针数据"
    print(f"[PASS] employee search 命中，当前共 {len(all_employees)} 条")

    # ---- tags_setting 闭环 ----
    api.upsert_tag(
        tag_id="probe_kam_client_tag001",
        tag_name="探针标签",
        deleted=False,
        strategy_id=0,
        group_id="probe_group",
        group_name="探针分组",
    )
    tag = api.get_tag("probe_kam_client_tag001")
    assert tag is not None and tag["tag_name"] == "探针标签", f"tag 回读不符：{tag}"
    print("[PASS] tags_setting upsert → get 闭环")

    # ---- 聚合查询容错：客户不存在时不应该抛异常 ----
    detail = api.get_customer_detail(
        follow_user_id="probe_kam_client_emp001",
        external_id="probe_kam_client_not_exist",
    )
    assert detail["external_user"] is None
    assert detail["profile"] is None
    assert detail["orders"] == []
    assert detail["tags"] == []
    print("[PASS] get_customer_detail 对不存在的客户返回干净的空结果，未抛异常")

    print("\n全部通过。probe_kam_client_* 探针数据按惯例暂留数据库，未清理。")


if __name__ == "__main__":
    main()
