"""登录态与角色权限装饰器。

对应 04号设计文档 决策1/决策2/决策4：
  - login_required：只校验"有没有登录"，不管角色。
  - require_role：显式声明某个路由允许哪些角色访问，写在路由函数上，
    与 login_required 配合使用（先登录校验，再角色校验）。
  - get_current_user()：统一从 session 读取当前用户信息并组装成 dict，
    所有路由函数通过它拿用户信息，不直接操作 session[...]——
    这样以后如果 session 存储结构调整（比如换成JWT），只需要改这一处。

数据范围过滤（"能看到哪些数据"）不在这个文件，见 permissions.py（04号文档
"为什么把 auth.py 和 permissions.py 拆成两个文件"一节）。
"""

from functools import wraps

from flask import jsonify, redirect, request, session, url_for


def login_required(f):
    """页面/API 路由通用：未登录跳转到登录页（页面路由）或返回401（API路由）。

    区分页面/API的依据：请求路径是否以 /api/ 开头——04号文档第五节约定
    API 路由未登录要返回 {"success": false, "error": "未登录"} + 401，
    不能跳转（前端 ajax 拿到的是 HTML 登录页会直接出错）。
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "未登录"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated_function


def require_role(*allowed_roles):
    """声明这个路由只允许哪些角色访问。必须配合 login_required 使用，
    且要写在 login_required 下面（离函数最近），保证先校验登录态、
    再校验角色——顺序写反的话，未登录用户会先撞上角色校验，得到的
    错误信息不准确（应该先提示"未登录"而不是"无权限"）。

    04号文档第五节：API路由角色不足返回403 + {"success": false, "error": "无权限"}；
    页面路由角色不足渲染一个"无权限访问"提示页（不是裸 HTTP 403）。
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            current_role = session.get("role")
            if current_role not in allowed_roles:
                if request.path.startswith("/api/"):
                    return jsonify({"success": False, "error": "无权限"}), 403
                return (
                    render_forbidden_page(),
                    403,
                )
            return f(*args, **kwargs)

        return decorated_function

    return decorator


def render_forbidden_page():
    """页面路由权限不足时的提示页。复用 base.html 布局，不是裸 HTTP 403
    （04号文档第五节）。用内联模板字符串而不是单独的 .html 文件——
    这个提示页内容极简、不会被复用到其他地方，没必要为它建模板文件。"""
    from flask import render_template_string

    return render_template_string(
        """
        {% extends "base.html" %}
        {% block title %}无权限访问{% endblock %}
        {% block content %}
        <div class="alert alert-warning mt-4">
            <i class="fas fa-lock"></i> 您没有权限访问此页面。
        </div>
        {% endblock %}
        """
    )


def get_current_user() -> dict | None:
    """从 session 组装当前用户信息。未登录返回 None。

    调用方（路由函数）应该优先用这个函数而不是直接读 session[...]，
    见04号文档决策4。
    """
    if "user_id" not in session:
        return None
    return {
        "user_id": session["user_id"],
        "name": session.get("name"),
        "role": session.get("role"),
        "region": session.get("region"),
    }
