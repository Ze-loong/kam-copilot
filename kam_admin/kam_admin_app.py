#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kam_admin —— Flask 管理后台入口。

提供登录鉴权、员工与客户管理、订单、标签、资料库、看板和画像采纳率页面。
功能权限由路由装饰器校验，客户数据按顾问与大区过滤。
"""

import os

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

from auth import get_current_user, login_required, require_role
from permissions import get_visible_follow_user_ids
from store_client import KamStoreAPIError, create_kam_store_api
from store_client.passwords import verify_password

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-not-for-production")

client = create_kam_store_api(base_url=os.getenv("KAM_AGENT_BASE_URL", "http://127.0.0.1:8000"))


# ==================== 登录鉴权 ====================


@app.route("/")
@login_required
def index():
    """首页按角色重定向。04号文档4.1节：普通顾问没有员工管理权限，
    固定跳 /employees 会立刻撞权限拒绝，体验不好——consultant 跳
    /customers，regional_manager/super_admin 跳 /employees。"""
    user = get_current_user()
    if user["role"] == "consultant":
        return redirect(url_for("customers"))
    return redirect(url_for("employees"))


@app.route("/login", methods=["GET", "POST"])
def login():
    """登录页。04号文档决策1：真实查 employee 表校验密码，不用环境变量
    写死账号——因为权限模型要求登录后知道当前用户的 role/region，
    单账号模式满足不了多角色场景。"""
    if request.method == "POST":
        user_id = request.form.get("user_id")
        password = request.form.get("password")

        if not user_id or not password:
            return render_template("login.html", error="请输入用户ID和密码")

        try:
            employee = client.get_employee(user_id)
        except KamStoreAPIError as exc:
            return render_template("login.html", error=f"连接 kam_agent 失败：{exc}")

        if (
            employee is None
            or employee.get("disabled", False)
            or not verify_password(password, employee.get("password"))
        ):
            return render_template("login.html", error="用户ID或密码错误")

        # 登录成功：一次性把 user_id/role/region/name 存进 session，
        # 后续路由函数从 session 读，不用每个请求都重新查 employee 表
        # （04号文档决策1，用换取请求效率）。
        session["user_id"] = employee["user_id"]
        session["name"] = employee["name"]
        session["role"] = employee["role"]
        session["region"] = employee["region"]

        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ==================== 员工管理（本轮实现） ====================


@app.route("/employees")
@login_required
@require_role("regional_manager", "super_admin")
def employees():
    """员工列表页。04号文档4.1节：普通顾问不能进。"""
    return render_template("employees.html")


@app.route("/api/employees", methods=["GET"])
@login_required
@require_role("regional_manager", "super_admin")
def api_employees():
    """获取员工列表，仅返回页面必需字段并执行区域过滤。"""
    try:
        data = [item for item in client.list_employees() if not item.get("disabled", False)]
        current_user = get_current_user()
        if current_user["role"] == "regional_manager":
            data = [item for item in data if item.get("region") == current_user.get("region")]
        safe_data = [
            {key: item.get(key) for key in ("user_id", "name", "role", "region")}
            for item in data
        ]
        return jsonify({"success": True, "data": safe_data})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/employees/<user_id>", methods=["DELETE"])
@login_required
@require_role("super_admin")
def api_disable_employee(user_id):
    """禁用员工账号，保留关联的客户归属和历史数据。"""
    if user_id == get_current_user()["user_id"]:
        return jsonify({"success": False, "error": "不能禁用当前登录账号"}), 400
    try:
        if not client.disable_employee(user_id):
            return jsonify({"success": False, "error": "员工不存在"}), 404
        return jsonify({"success": True, "message": "员工账号已禁用"})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/employees", methods=["POST"])
@login_required
@require_role("super_admin")
def api_create_employee():
    """新增员工API。04号文档4.2节：权限收紧到 super_admin only——
    区域主管如果能新增员工，理论上可以新增其他区域的账号，超出
    "区域主管只管理自己区域"的直觉边界，是本文档在需求文字之外
    做的合理收紧判断。"""
    try:
        data = request.get_json(silent=True) or {}
        user_id = data.get("user_id")
        name = data.get("name")
        password = data.get("password")
        role = data.get("role")
        region = data.get("region")

        if not all([user_id, name, password, role, region]):
            return jsonify({"success": False, "error": "user_id/name/password/role/region 均不能为空"})

        if role not in ("consultant", "regional_manager", "super_admin"):
            return jsonify({"success": False, "error": f"未知角色：{role}"})

        client.upsert_employee(user_id=user_id, name=name, role=role, region=region, password=password)
        return jsonify({"success": True, "message": "员工创建成功"})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


# ==================== 客户管理（本轮实现） ====================
# 04号文档决策3：应用层过滤。本模块是唯一真正用到 permissions.py 的地方——
# 员工管理是角色级"能不能进"，客户管理是数据级"能看到哪些"。


@app.route("/customers")
@login_required
def customers():
    """客户列表页。全角色可进，具体能看到哪些客户由 API 侧的数据范围过滤决定，
    页面本身不做权限拦截（04号文档路由表4.1节：/customers 权限=全角色）。"""
    return render_template("employee_customers.html")


def _can_view_follow_user_id(follow_user_id: str) -> bool:
    """当前登录用户是否有权查看指定顾问名下的客户。"""
    visible_ids = get_visible_follow_user_ids(get_current_user(), client)
    return visible_ids is None or follow_user_id in visible_ids


@app.route("/employee_customers/<user_id>")
@login_required
@require_role("regional_manager", "super_admin")
def employee_customers(user_id):
    """从员工列表进入，只展示被点击员工名下的客户。"""
    if not _can_view_follow_user_id(user_id):
        abort(403)
    return render_template("employee_customers.html", selected_follow_user_id=user_id)


@app.route("/customers/<external_id>")
@login_required
def customer_detail(external_id):
    """客户详情页与详情 API 使用相同的数据范围校验。"""
    customer, _ = _find_customer_and_owner(external_id)
    if customer is None:
        abort(403)
    return render_template("customer_detail.html", external_id=external_id)


@app.route("/orders/<union_id>")
@login_required
def customer_orders(union_id):
    if not any(c.get("union_id") == union_id for c in _get_visible_customers()):
        abort(403)
    return render_template("customer_orders.html", union_id=union_id)


@app.route("/tags")
@login_required
@require_role("regional_manager", "super_admin")
def tags():
    """标签体系页。实际数据由前端 ajax 调 /api/tags 拉取渲染。"""
    return render_template("tags.html")


@app.route("/dashboard")
@login_required
@require_role("regional_manager", "super_admin")
def dashboard():
    """数据看板页。实际数据由前端 ajax 调 /api/dashboard 拉取渲染。"""
    return render_template("dashboard.html")


@app.route("/ai_adoption_rate")
@login_required
@require_role("regional_manager", "super_admin")
def ai_adoption_rate():
    """AI采纳率统计页。实际数据由前端 ajax 调 /api/ai_adoption_rate 拉取渲染。"""
    return render_template("ai_adoption_rate.html")


@app.route("/system_config")
@login_required
@require_role("super_admin")
def system_config():
    """全局系统配置页（，02号架构文档7.5节）：F17跨模块综合
    推理的全局关停开关。限super_admin——这是影响全公司所有顾问的开关，
    不是常规业务操作，权限收紧到跟员工管理同一级别。实际数据由前端 ajax
    调 /api/system_config 拉取渲染，跟本文件其余页面同一套模式。"""
    return render_template("system_config.html")


def _get_visible_customers() -> list[dict]:
    """当前用户可见范围内的全部客户，每条附带 follow_user_id。

    拿到可见顾问列表后，通过一次内部批量 HTTP 请求查回各顾问的客户，
    再在应用层按已经校验过的顾问范围组装结果。
    """
    user = get_current_user()
    visible_ids = get_visible_follow_user_ids(user, client)
    if visible_ids is None:
        # super_admin：无原生"不限定顾问查全部客户"方式（04号文档第六节已知限制）
        visible_ids = [e["user_id"] for e in client.list_employees()]

    return client.list_external_users_batch(visible_ids)


def _find_customer_and_owner(external_id: str) -> tuple[dict | None, str | None]:
    """跨顾问查一个客户属于谁。

    external_user 命名空间必须带 follow_user_id 才能 get/search（04号文档
    第六节已知限制），没有"不知道顾问是谁、直接按客户ID查"的原生方式。
    这里的做法是：拿到当前用户可见的顾问列表，批量查回客户后
    查找匹配的 external_id。如果客户不在可见范围内，自然返回
    (None, None)，调用方据此返回403。
    """
    for c in _get_visible_customers():
        if c.get("external_id") == external_id:
            return c, c["follow_user_id"]
    return None, None


@app.route("/api/customers", methods=["GET"])
@login_required
def api_customers():
    """客户列表API。04号文档决策3：应用层过滤。"""
    try:
        return jsonify({"success": True, "data": _get_visible_customers()})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/employee_customers/<user_id>", methods=["GET"])
@login_required
@require_role("regional_manager", "super_admin")
def api_employee_customers(user_id):
    """按员工查询客户，并在服务端校验区域/角色数据范围。"""
    if not _can_view_follow_user_id(user_id):
        return jsonify({"success": False, "error": "无权限查看该员工的客户"}), 403

    try:
        data = client.list_external_users(user_id)
        for customer in data:
            customer["follow_user_id"] = user_id
        return jsonify({"success": True, "data": data})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/customers/<external_id>", methods=["GET"])
@login_required
def api_customer_detail(external_id):
    """客户详情API。04号文档第五节：越权和不存在统一返回403，不区分
    "无权限"和"数据不存在"——避免攻击者通过响应差异探测 external_id
    是否真实存在。"""
    try:
        customer, follow_user_id = _find_customer_and_owner(external_id)
        if customer is None:
            return jsonify({"success": False, "error": "无权限或客户不存在"}), 403

        detail = client.get_customer_detail(follow_user_id, external_id)
        detail["follow_user_id"] = follow_user_id
        return jsonify({"success": True, "data": detail})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/orders/<union_id>", methods=["GET"])
@login_required
def api_customer_orders(union_id):
    """订单列表API。订单命名空间只按 union_id 分区，没有直接的顾问归属，
    所以越权校验要先反查"这个 union_id 属于哪个客户/顾问"——遍历可见范围内
    的客户，匹配 union_id 命中才放行，同样统一返回403（不区分无权限/不存在）。"""
    try:
        owned = any(c.get("union_id") == union_id for c in _get_visible_customers())

        if not owned:
            return jsonify({"success": False, "error": "无权限或订单不存在"}), 403

        orders = client.list_orders(union_id)
        return jsonify({"success": True, "data": orders})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/tags", methods=["GET", "POST"])
@login_required
@require_role("regional_manager", "super_admin")
def api_tags():
    """标签体系API。04号文档路由表4.2节：/api/tags GET/POST 均为
    regional_manager/super_admin 权限（不像客户管理那样需要数据范围过滤——
    标签是全公司统一的配置数据，不按顾问/区域分区，见02号文档 tags_setting
    命名空间设计：单层namespace，没有 follow_user_id 这一层）。"""
    if request.method == "GET":
        try:
            data = client.list_tags()
            return jsonify({"success": True, "data": data})
        except KamStoreAPIError as exc:
            return jsonify({"success": False, "error": str(exc)})

    # POST：新增/编辑标签
    try:
        data = request.get_json(silent=True) or {}
        tag_id = data.get("tag_id")
        tag_name = data.get("tag_name")
        group_id = data.get("group_id")
        group_name = data.get("group_name")
        strategy_id = data.get("strategy_id")
        deleted = data.get("deleted", False)

        if not all([tag_id, tag_name, group_id, group_name]) or strategy_id is None:
            return jsonify({"success": False, "error": "tag_id/tag_name/group_id/group_name/strategy_id 均不能为空"})

        client.upsert_tag(
            tag_id=tag_id,
            tag_name=tag_name,
            deleted=bool(deleted),
            strategy_id=int(strategy_id),
            group_id=group_id,
            group_name=group_name,
        )
        return jsonify({"success": True, "message": "标签保存成功"})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/tags/suggestions/<external_id>", methods=["GET"])
@login_required
def api_tag_suggestions(external_id):
    """标签推荐结果展示API。

    F04 角色分工：02号架构文档295行
    原始定位就是"展示kam_agent生成的标签推荐结果，admin只读取展示"，
    真正触发AI推荐生成+人工确认是 kam_sidebar 的职责（复用画像生成同款
    /tasks/generate_tags → SSE → /tasks/confirm_tags 三段式交互，见
    设计文档/08-F04标签推荐设计文档.md）。kam_admin不触发生成，也不做
    单独的"推荐历史"持久化（F04确认后只把最终结果合并进 external_user.tags，
    没有像F01画像字段那样保留status留痕，这是本轮范围内的已知限制，
    如实记录，不在这里假装有"推荐历史"可展示）。

    所以这里只读客户当前标签（confirm_tag_batch写回的最终结果），复用
    与 /api/customers/<external_id> 完全相同的越权校验和数据获取逻辑
    （_find_customer_and_owner + get_customer_detail），不新增数据路径。
    """
    try:
        customer, follow_user_id = _find_customer_and_owner(external_id)
        if customer is None:
            return jsonify({"success": False, "error": "无权限或客户不存在"}), 403

        detail = client.get_customer_detail(follow_user_id, external_id)
        return jsonify({"success": True, "data": {"tags": detail.get("tags", [])}})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


def _iter_visible_profile_items(customers: list[dict] | None = None):
    """遍历当前用户可见范围内所有客户的画像字段，逐条 yield (field_name, item_dict)。

    客户画像由 kam_client 在一次批量 HTTP 请求中读取。看板可以把
    已经取得的 customers 传进来，避免重复查客户列表。
    """
    customers = customers if customers is not None else _get_visible_customers()
    profiles = client.get_customer_profiles_batch(customers)
    for profile in profiles:
        if not profile:
            continue
        for field_name, item in (profile.get("profile_items") or {}).items():
            yield field_name, item


@app.route("/api/dashboard", methods=["GET"])
@login_required
@require_role("regional_manager", "super_admin")
def api_dashboard():
    """数据看板API。04号文档六.5节：四项指标均从当前真实数据算出，
    不还原需求文档字面的转化漏斗/续费率（数据结构不支持，见文档说明）。"""
    try:
        customers = _get_visible_customers()
        customer_count = len(customers)

        order_map = client.list_orders_batch(
            [customer.get("union_id") for customer in customers]
        )
        customers_with_orders = 0
        order_count = 0
        for c in customers:
            orders = order_map.get(c.get("union_id"), [])
            order_count += len(orders)
            if orders:
                customers_with_orders += 1

        order_conversion_rate = (
            round(customers_with_orders / customer_count, 4) if customer_count else 0.0
        )

        profile_status_counts = {
            "draft": 0, "need_verify": 0, "confirmed": 0,
            "discarded": 0, "need_regenerate": 0,
        }
        for _field_name, item in _iter_visible_profile_items(customers):
            status = item.get("status")
            if status in profile_status_counts:
                profile_status_counts[status] += 1

        customers_by_owner: dict[str, int] = {}
        for c in customers:
            follow_user_id = c["follow_user_id"]
            customers_by_owner[follow_user_id] = customers_by_owner.get(follow_user_id, 0) + 1

        return jsonify({
            "success": True,
            "data": {
                "customer_count": customer_count,
                "order_count": order_count,
                "customers_with_orders": customers_with_orders,
                "order_conversion_rate": order_conversion_rate,
                "profile_status_counts": profile_status_counts,
                "customers_by_owner": customers_by_owner,
            },
        })
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/ai_adoption_rate", methods=["GET"])
@login_required
@require_role("regional_manager", "super_admin")
def api_ai_adoption_rate():
    """AI采纳率统计API。04号文档六.5节：只做画像维度（当前只有画像字段的确认结果可用于计算），回复、标签尚未记录采纳结果，
    日程提醒尚未实现。

    采纳率口径：confirmed / (confirmed + discarded)，draft/need_verify/
    need_regenerate 这些"还没做终态决定"的状态不计入分母（会稀释语义）。
    """
    try:
        confirmed = 0
        discarded = 0
        pending = 0  # draft + need_verify + need_regenerate，辅助信息不参与采纳率计算
        for _field_name, item in _iter_visible_profile_items():
            status = item.get("status")
            if status == "confirmed":
                confirmed += 1
            elif status == "discarded":
                discarded += 1
            elif status in ("draft", "need_verify", "need_regenerate"):
                pending += 1

        decided_total = confirmed + discarded
        profile_adoption_rate = round(confirmed / decided_total, 4) if decided_total else None

        return jsonify({
            "success": True,
            "data": {
                "profile": {
                    "implemented": True,
                    "confirmed": confirmed,
                    "discarded": discarded,
                    "pending": pending,
                    "adoption_rate": profile_adoption_rate,
                },
                "reply_suggestion": {"implemented": False, "reason": "回复建议尚未埋点记录采纳结果，暂不统计"},
                "tag_suggestion": {"implemented": False, "reason": "标签推荐尚未埋点记录采纳结果，暂不统计"},
                "schedule_reminder": {"implemented": False, "reason": "kam_agent 尚未实现日程提醒子图"},
            },
        })
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


# ==================== 资料库管理（见02号架构文档7.4节） ====================
# 权限比照标签体系（regional_manager+super_admin）——资料内容会影响
# F16/F17回答给所有客户的内容，判断为需要收紧到运营管理层，不是普通顾问
# 能碰的模块；如需资料维护专岗，可再拆分角色。

_KB_CATEGORIES = [
    ("profile", "集团概况"),
    ("solution", "产品与解决方案"),
    ("honor", "荣誉资质"),
    ("faq", "常见FAQ"),
]


@app.route("/kb")
@login_required
@require_role("regional_manager", "super_admin")
def kb():
    """资料库管理页。列表+上传合并在一个页面（跟 /tags 同一种"列表+新增
    模态框"模式），实际数据由前端 ajax 调 /api/kb/list 拉取渲染。"""
    return render_template("kb.html", categories=_KB_CATEGORIES)


@app.route("/api/kb/list", methods=["GET"])
@login_required
@require_role("regional_manager", "super_admin")
def api_kb_list():
    """资料库文档列表API。02号架构文档7.4节 `/kb/list`——kam_admin不直连
    向量库，这里只是转发 kam_agent 的 /kb/documents 查询结果。"""
    try:
        return jsonify({"success": True, "data": client.list_kb_documents()})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/kb/detail", methods=["GET"])
@login_required
@require_role("regional_manager", "super_admin")
def api_kb_detail():
    """资料内容详情API（）——"查看"功能用，把某一份文档的
    完整内容（各片段拼接）返回，前端用来展示、并一键灌进上传框改完重传。
    source_doc走query string而不是URL路径段——避免Flask路由对中文路径
    段解码这个不确定因素，跟 /api/kb/list 等既有GET接口的参数风格一致。"""
    source_doc = (request.args.get("source_doc") or "").strip()
    if not source_doc:
        return jsonify({"success": False, "error": "缺少 source_doc 参数"})
    try:
        content = client.get_kb_document_content(source_doc)
        return jsonify({"success": True, "data": {"content": content}})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/kb/upload", methods=["POST"])
@login_required
@require_role("regional_manager", "super_admin")
def api_kb_upload():
    """资料上传API。02号架构文档7.4节 `/kb/upload`——支持直接粘贴文本，
    或上传.txt文件（服务端按UTF-8解码后与粘贴文本走同一条路径）。

    只支持.txt：项目里没有PDF/Word解析依赖，真要支持得先加
    python-docx/pypdf这类新依赖，超出当前范围——如果运营人员的资料是其他
    格式，先手动转成txt再上传。同名source_doc视为替换（kam_agent侧
    ingest_document()内部实现，这里不重复判断）。
    """
    source_doc = (request.form.get("source_doc") or "").strip()
    category = (request.form.get("category") or "").strip()
    content = (request.form.get("content") or "").strip()

    upload_file = request.files.get("file")
    if upload_file and upload_file.filename:
        try:
            content = upload_file.read().decode("utf-8").strip()
        except UnicodeDecodeError:
            return jsonify({"success": False, "error": "文件不是UTF-8编码的文本，请转成.txt后重试"})

    valid_categories = {c for c, _ in _KB_CATEGORIES}
    if category not in valid_categories:
        return jsonify({"success": False, "error": f"未知类别：{category}"})
    if not source_doc or not content:
        return jsonify({"success": False, "error": "文档名称和内容均不能为空（粘贴文本或选择文件二选一）"})

    try:
        chunk_count = client.upload_kb_document(source_doc=source_doc, category=category, content=content)
        return jsonify({"success": True, "data": {"chunk_count": chunk_count}})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/system_config", methods=["GET", "POST"])
@login_required
@require_role("super_admin")
def api_system_config():
    """F17全局关停开关API。GET查当前状态，POST切换——跟 /api/tags 同一套
    GET/POST合一路由的约定。关闭后 kam_agent 侧 F17会自动降级走F16知识库
    问答兜底（见kam_agent的webapp.py `_stream_reasoning_events`），这里
    只负责读写开关状态本身，不关心降级逻辑。"""
    if request.method == "GET":
        try:
            return jsonify({"success": True, "data": {"reasoning_enabled": client.get_reasoning_enabled()}})
        except KamStoreAPIError as exc:
            return jsonify({"success": False, "error": str(exc)})

    try:
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"success": False, "error": "enabled 字段必须是布尔值"})
        client.set_reasoning_enabled(enabled)
        return jsonify({"success": True, "data": {"reasoning_enabled": enabled}})
    except KamStoreAPIError as exc:
        return jsonify({"success": False, "error": str(exc)})


if __name__ == "__main__":
    app.run(
        debug=os.getenv("FLASK_DEBUG", "False").lower() == "true",
        host="127.0.0.1",
        port=5001,
    )
