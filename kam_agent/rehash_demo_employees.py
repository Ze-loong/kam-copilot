"""把3个demo员工账号的密码从明文重写为哈希格式，密码值本身不变。

背景：kam_admin登录密码校验此前为兼容历史明文账号而保留了明文比对分支，
改为只接受哈希格式后，需要先把仍是明文的旧记录用同一个
密码值重新走一遍 upsert_employee()（写入时自动哈希），再撤掉兼容分支，
这样登录凭据（账号名+密码）对使用者完全无感、不需要记新密码。

只处理这3个已知的demo账号，不触碰客户/聊天/订单等其他mock数据，
比重跑完整的 mock_data_init.py 更小范围、更安全。

运行方式（kam_agent 目录下）：
    uv run python rehash_demo_employees.py
"""

import os

from dotenv import load_dotenv
from langgraph.store.postgres import PostgresStore

from src.store.store_client import get_employee, upsert_employee

load_dotenv()

_ACCOUNTS = [
    {"user_id": "demo_emp_xiaozhang", "name": "小张", "role": "consultant", "region": "华东"},
    {"user_id": "demo_emp_manager", "name": "李经理", "role": "regional_manager", "region": "华东"},
    {"user_id": "demo_emp_admin", "name": "超级管理员", "role": "super_admin", "region": "全国"},
]
_PASSWORD = "demo123"  # 与 mock_data_init.py 中的原始明文密码值保持一致，不改变登录凭据


def main() -> None:
    conn_string = os.environ["KAM_POSTGRES_URL"]
    with PostgresStore.from_conn_string(conn_string) as store:
        store.setup()
        for account in _ACCOUNTS:
            before = get_employee(account["user_id"], store=store)
            if before is None:
                print(f"跳过：{account['user_id']} 不存在（可能尚未初始化过mock数据）")
                continue
            before_password = before.value.get("password", "")
            already_hashed = before_password.startswith("pbkdf2_sha256$")

            upsert_employee(
                user_id=account["user_id"],
                name=account["name"],
                role=account["role"],
                region=account["region"],
                password=_PASSWORD,
                store=store,
            )
            after = get_employee(account["user_id"], store=store)
            after_password = after.value.get("password", "")
            ok = after_password.startswith("pbkdf2_sha256$")

            status = "已是哈希，本次重写幂等覆盖" if already_hashed else "已从明文转为哈希"
            print(f"{account['user_id']}（{account['name']}）：{status}，验证结果={'通过' if ok else '失败'}")


if __name__ == "__main__":
    main()
