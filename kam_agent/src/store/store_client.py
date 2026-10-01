import datetime
import threading

from langgraph.config import get_store
from src.models.kam_models import NEED_VERIFY_THRESHOLD
from src.security.passwords import hash_password

# ：
# LangGraph Store 的 store.search() 不传 limit 时有默认上限（真实复现：
# tags_setting 命名空间明明写入了31条，search_tags_setting() 不传limit
# 拿回来只有10条，导致标签目录被静默截断，F04移除推荐候选因此缺失）。
# 这不是 tags_setting 独有的问题——本文件其余5个 search_* 函数同样没传
# limit，同样会被截断。风险最高的是 search_wxqy_msg/search_wxkf_msg/
# search_wxxd_order 三个：它们是 F01画像生成 和 F04标签推荐 共用的LLM
# 证据数据源，消息/订单数量一旦超过默认上限，LLM看到的证据会被静默减少，
# 且不会有任何报错提示——这比标签目录截断更隐蔽、影响更大。
# 统一给这几个数据量可能增长的命名空间设一个远高于当前mock数据规模的
# 上限（默认1000），正式生产环境如果数据量级远超这个数字，需要改成真正
# 的分页方案，这里先解决"默认上限吃掉真实数据"这个更紧迫的问题。
_DEFAULT_SEARCH_LIMIT = 1000


# external_user
def upsert_external_user(external_id, union_id, follow_user_id, name, remark_name, tags, store = None):
    if store is None:
        store = get_store()
    store.put(
        ("external_user", follow_user_id),
        external_id,
        {"external_id": external_id,
        "union_id": union_id,
        "follow_user_id": follow_user_id,
        "name": name,
        "remark_name": remark_name,
        "tags": tags}
    )

def get_external_user(follow_user_id, external_id, store = None):
    if store is None:
        store = get_store()
    result = store.get(("external_user", follow_user_id), external_id)
    return result

def search_external_user(follow_user_id, store = None, limit = _DEFAULT_SEARCH_LIMIT):
    if store is None:
        store = get_store()
    search_result = store.search(("external_user", follow_user_id), limit=limit)
    return search_result

# employee 员工
def upsert_employee(user_id, name, role, region, password, store = None):
    """写入员工记录，密码在落库前转为 PBKDF2-SHA256 哈希。"""
    if store is None:
        store = get_store()
    store.put(
        ("employee", ),
        user_id,
        {
            "user_id": user_id,
            "name": name,
            "role": role,
            "region": region,
            "password": hash_password(password),
            "disabled": False,
        }
    )

def get_employee(user_id, store = None):
    if store is None:
        store = get_store()
    result = store.get(("employee",), user_id)
    return result

def search_employee(store = None, limit = _DEFAULT_SEARCH_LIMIT):
    if store is None:
        store = get_store()
    search_result = store.search(("employee", ), limit=limit)
    return search_result

# tags_setting 标签设置
def upsert_tags_setting(tag_id, tag_name, deleted, strategy_id, group_id, group_name, store = None):
    if store is None:
        store = get_store()
    store.put(
        ("tags_setting", ),
        tag_id,
        {"tag_name" : tag_name,
        "deleted": deleted,
        "strategy_id": strategy_id,
        "group_id": group_id,
        "group_name": group_name}
    )

def get_tags_setting(tag_id, store = None):
    if store is None:
        store = get_store()
    result = store.get(("tags_setting",), tag_id)
    return result

def search_tags_setting(store = None, limit = _DEFAULT_SEARCH_LIMIT):
    if store is None:
        store = get_store()
    search_result = store.search(("tags_setting", ), limit=limit)
    return search_result

# wxkf_msg 微信客服
def  upsert_wxkf_msg(external_id, msg_id, content, msg_time, origin, YYYYMMDD, store = None):
    if store is None:
        store = get_store()
    store.put(
        ("wxkf_msg", external_id),
        msg_id,
        {
        "content": content,
        "msg_time": msg_time,
        "origin": origin,
        "YYYYMMDD": YYYYMMDD,
        }
    )

def get_wxkf_msg(external_id, msg_id, store = None):
    if store is None:
        store = get_store()
    result = store.get(
        ("wxkf_msg", external_id),
        msg_id,
    )
    return result

def search_wxkf_msg(external_id, store = None, limit = _DEFAULT_SEARCH_LIMIT):
    if store is None:
        store = get_store()
    result = store.search(
        ("wxkf_msg", external_id),
        limit=limit,
    )
    return result

# wxxd_order 客户订单记录
def upsert_wxxd_order(union_id, order_id, order_products, order_create_time, YYYYMMDD, store = None):
    if store is None:
        store = get_store()
    store.put(
        ("wxxd_order", union_id),
        order_id,
        {"order_products":order_products,
        "order_create_time": order_create_time,
        "YYYYMMDD": YYYYMMDD
        }
    )

def get_wxxd_order(union_id, order_id, store = None):
    if store is None:
        store = get_store()
    result = store.get(
        ("wxxd_order", union_id),
        order_id,
    )
    return result

def search_wxxd_order(union_id, store = None, limit = _DEFAULT_SEARCH_LIMIT):
    if store is None:
        store = get_store()
    result = store.search(
        ("wxxd_order", union_id),
        limit=limit,
    )
    return result

# wxqy_msg（sorted_from_to_key拼接） 企微聊天消息
# 先拼接客户与顾问id
def _sorted_from_to_key(id_a, id_b):
    return "".join(sorted([id_a, id_b]))

def  upsert_wxqy_msg(follow_user_id, external_id, msg_id, from_id, to_id, content, msg_time, YYYYMMDD, store=None):
    if store is None:
        store = get_store()
    store.put(
        ("wxqy_msg", _sorted_from_to_key(follow_user_id, external_id)),
        msg_id,
        {
        "from_id": from_id,
        "to_id": to_id,
        "content": content,
        "msg_time": msg_time,
        "YYYYMMDD": YYYYMMDD,
        }
    )

def get_wxqy_msg(follow_user_id, external_id, msg_id, store = None):
    if store is None:
        store = get_store()
    result = store.get(
        ("wxqy_msg", _sorted_from_to_key(follow_user_id, external_id)),
        msg_id,
    )
    return result

def search_wxqy_msg(follow_user_id, external_id, store = None, limit = _DEFAULT_SEARCH_LIMIT):
    if store is None:
        store = get_store()
    result = store.search(
        ("wxqy_msg", _sorted_from_to_key(follow_user_id, external_id)),
        limit=limit,
    )
    return result

# external_user_profile 用户画像

# 同一客户的画像确认会执行“读旧值→合并→写回”。字段子图可并发
# 恢复，因此必须将这个闭环串行化，否则两个字段可能互相覆盖。
_profile_locks: dict[tuple[str, str], threading.RLock] = {}
_profile_locks_guard = threading.Lock()


def _get_profile_lock(follow_user_id: str, external_id: str) -> threading.RLock:
    """获取当前进程内某个客户专属的重入锁。"""
    lock_key = (follow_user_id, external_id)
    with _profile_locks_guard:
        return _profile_locks.setdefault(lock_key, threading.RLock())

def upsert_external_user_profile(follow_user_id, external_id, new_profile_items, store=None):
    if store is None:
        store = get_store()

    # 锁必须包住整个读—合并—写闭环，只锁 put 无法防止丢更新。
    with _get_profile_lock(follow_user_id, external_id):
        _upsert_external_user_profile_unlocked(
            follow_user_id,
            external_id,
            new_profile_items,
            store,
        )


def _upsert_external_user_profile_unlocked(
    follow_user_id, external_id, new_profile_items, store
):
    """执行画像合并；调用方必须先持有该客户的专属锁。"""

    # 第1步：拿旧数据
    old_data = store.get(("external_user_profile", follow_user_id), external_id)

    # 第2步：给新字段打 need_verify 标记
    for field_name, field_data in new_profile_items.items():
        confidence = field_data.get("confidence")

        if (
            field_data.get("status") == "draft"
            and confidence is not None
            and confidence <= NEED_VERIFY_THRESHOLD
        ):
            field_data["status"] = "need_verify"

    # 第3、4步：合并
    if old_data is None:
        # 旧数据不存在，新草稿直接就是最终结果
        merged_items = new_profile_items
    else:
        old_items = old_data.value["profile_items"]
        merged_items = dict(old_items)  # 先复制一份旧的做底
        for field_name, field_data in new_profile_items.items():
            old_field = old_items.get(field_name)
            if old_field is not None and old_field["status"] == "confirmed":
                continue  # 已确认，跳过，不覆盖
            merged_items[field_name] = field_data  # 否则用新的覆盖（或新增）

    # 第5步：写回去
    store.put(
        ("external_user_profile", follow_user_id),
        external_id,
        {
            "profile_items": merged_items,
            "timeline": old_data.value["timeline"] if old_data else [],
            "updated_at": datetime.datetime.now().isoformat()
        }
    )

def get_external_user_profile(follow_user_id, external_id, store=None):
    if store is None:
        store = get_store()
    result = store.get(("external_user_profile", follow_user_id), external_id)
    return result

def search_external_user_profile(follow_user_id, store=None, limit = _DEFAULT_SEARCH_LIMIT):
    if store is None:
        store = get_store()
    result = store.search(("external_user_profile", follow_user_id), limit=limit)
    return result

# system_config 系统配置（，F17全局关停开关，见02号架构文档7.5节）
def get_reasoning_enabled(store=None) -> bool:
    """F17综合推理全局开关，Store里没有记录时默认true（架构文档7.5节：
    "值为布尔型，默认true"）。单独封装成语义化的读/写两个函数而不是走
    通用get/upsert，因为这是唯一一个"只有一个固定key、调用方不需要关心
    namespace/key细节"的配置项，跟其他7个命名空间"多条记录"的结构不同，
    没必要强行套同一套三件套（upsert/get/search）模式。
    """
    if store is None:
        store = get_store()
    result = store.get(("system_config",), "reasoning_enabled")
    if result is None:
        return True
    return bool(result.value.get("enabled", True))


def set_reasoning_enabled(enabled: bool, store=None) -> None:
    if store is None:
        store = get_store()
    store.put(("system_config",), "reasoning_enabled", {"enabled": enabled})
