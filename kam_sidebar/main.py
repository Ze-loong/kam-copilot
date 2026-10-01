"""
kam_sidebar —— FastAPI 侧边栏入口。

提供顾问登录、画像生成与确认、回复建议、标签推荐、知识库问答和综合推理接口。
日程提醒仍为界面占位。
"""

import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel

from kam_agent_client import KamAgentAPIError, create_kam_agent_client
from passwords import verify_password

load_dotenv()

app = FastAPI(title="kam_sidebar")

client = create_kam_agent_client(base_url=os.getenv("KAM_AGENT_BASE_URL", "http://127.0.0.1:8000"))

# ---------------------------------------------------------------------------
# 登录态：轻量签名 Cookie（不是 Flask session，FastAPI 没有内置等价物）。
#
# 06号设计文档决策1：kam_sidebar 场景单一（顾问只操作自己名下客户），登录
# 校验通过只需要记住 user_id/name/role/region 供页面展示用，不需要像
# kam_admin 那样做多层数据范围过滤，所以没有理由引入 SessionMiddleware
# 这类更重的方案——用 itsdangerous 签名一个 JSON 字符串放进 Cookie，
# 每个请求自己验签解析，效果等价于 session，依赖更少。
# ---------------------------------------------------------------------------

_serializer = URLSafeSerializer(os.getenv("SESSION_SECRET_KEY", "dev-secret-key-not-for-production"))
_COOKIE_NAME = "kam_sidebar_session"


def _get_current_user(request: Request) -> dict | None:
    raw = request.cookies.get(_COOKIE_NAME)
    if not raw:
        return None
    try:
        return _serializer.loads(raw)
    except BadSignature:
        return None


def _require_login(request: Request) -> dict:
    user = _get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    user_id: str
    password: str


@app.post("/login")
async def login(payload: LoginRequest):
    """查 employee 校验密码。06号文档决策1：与 kam_admin 同款校验逻辑，
    独立实现，走 /store/get 通用接口。"""
    try:
        employee = await client.get_employee(payload.user_id)
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=f"连接 kam_agent 失败：{exc}") from exc

    if (
        employee is None
        or employee.get("disabled", False)
        or not verify_password(payload.password, employee.get("password"))
    ):
        raise HTTPException(status_code=401, detail="用户ID或密码错误")

    session_data = {
        "user_id": employee["user_id"],
        "name": employee["name"],
        "role": employee["role"],
        "region": employee["region"],
    }
    cookie_value = _serializer.dumps(session_data)

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(_COOKIE_NAME, cookie_value, httponly=True, samesite="lax")
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse(url="/static/login.html", status_code=303)
    response.delete_cookie(_COOKIE_NAME)
    return response


@app.get("/api/me")
async def api_me(request: Request):
    """前端页面加载时用这个接口拿当前登录人信息（姓名展示、拼 follow_user_id）。"""
    user = _require_login(request)
    return {"success": True, "data": user}


# ---------------------------------------------------------------------------
# /api/generate_profile —— 转发 /tasks/generate_profile
# ---------------------------------------------------------------------------


class GenerateProfileRequest(BaseModel):
    external_id: str


@app.post("/api/generate_profile")
async def api_generate_profile(payload: GenerateProfileRequest, request: Request):
    user = _require_login(request)
    try:
        thread_id = await client.generate_profile(
            follow_user_id=user["user_id"], external_id=payload.external_id
        )
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"success": True, "data": {"thread_id": thread_id}}


# ---------------------------------------------------------------------------
# /api/stream/{thread_id} —— 消费 kam_agent SSE，重新组织后转发给前端
# ---------------------------------------------------------------------------


def _format_sse(event: str, data: dict) -> str:
    import json

    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _relay_profile_events(thread_id: str, follow_user_id: str, external_id: str):
    """06号文档决策2：消费再转发。每条 profile_field_update 事件补充
    follow_user_id/external_id（kam_agent 的事件本身不带这两个字段，
    前端需要知道"当前是哪个客户"），done 事件原样转发。"""
    try:
        async for evt in client.stream_profile_events(thread_id, follow_user_id, external_id):
            event_name = evt["event"]
            data: dict[str, Any] = dict(evt["data"])

            if event_name == "profile_field_update":
                data["follow_user_id"] = follow_user_id
                data["external_id"] = external_id

            yield _format_sse(event_name, data)
    except KamAgentAPIError as exc:
        yield _format_sse("error", {"message": str(exc)})


@app.get("/api/stream/{thread_id}")
async def api_stream(thread_id: str, external_id: str, request: Request):
    user = _require_login(request)
    return StreamingResponse(
        _relay_profile_events(thread_id, user["user_id"], external_id),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# /api/confirm —— 透传 /tasks/confirm
# ---------------------------------------------------------------------------


class ConfirmRequest(BaseModel):
    thread_id: str  # 字段自己的子 thread_id，来自 SSE 事件（06号文档决策3说明）
    external_id: str
    field: str
    interrupt_id: str
    action: str  # ok / discard / recreate


@app.post("/api/confirm")
async def api_confirm(payload: ConfirmRequest, request: Request):
    user = _require_login(request)
    try:
        result = await client.confirm_field(
            thread_id=payload.thread_id,
            follow_user_id=user["user_id"],
            external_id=payload.external_id,
            field=payload.field,
            interrupt_id=payload.interrupt_id,
            action=payload.action,
        )
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"success": True, "data": result}


# ---------------------------------------------------------------------------
# F04标签推荐三件套（ 收尾）
#
# 与画像三件套结构对称，但内部语义不同：整批一次interrupt（不是逐字段
# 循环），confirm提交的是两个id列表（不是单个action）。真正触发生成+
# 确认是 kam_sidebar 的职责（对齐02号架构文档"admin只读展示"的定位，
# 见 实战记录.md  条目的角色分工判断）。
# ---------------------------------------------------------------------------


class GenerateTagsRequest(BaseModel):
    external_id: str


@app.post("/api/generate_tags")
async def api_generate_tags(payload: GenerateTagsRequest, request: Request):
    user = _require_login(request)
    try:
        thread_id = await client.generate_tags(
            follow_user_id=user["user_id"], external_id=payload.external_id
        )
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"success": True, "data": {"thread_id": thread_id}}


async def _relay_tag_events(thread_id: str, follow_user_id: str, external_id: str):
    """消费再转发，和 _relay_profile_events 同一个模式。tag_batch_update
    事件补充 follow_user_id/external_id 供前端使用；done 原样转发。"""
    try:
        async for evt in client.stream_tag_events(thread_id, follow_user_id, external_id):
            event_name = evt["event"]
            data: dict[str, Any] = dict(evt["data"])

            if event_name == "tag_batch_update":
                data["follow_user_id"] = follow_user_id
                data["external_id"] = external_id

            yield _format_sse(event_name, data)
    except KamAgentAPIError as exc:
        yield _format_sse("error", {"message": str(exc)})


@app.get("/api/stream_tags/{thread_id}")
async def api_stream_tags(thread_id: str, external_id: str, request: Request):
    user = _require_login(request)
    return StreamingResponse(
        _relay_tag_events(thread_id, user["user_id"], external_id),
        media_type="text/event-stream",
    )


class ConfirmTagsRequest(BaseModel):
    # 必须是SSE事件里 tag_batch_update.thread_id 带的"标签子图自己的"
    # thread_id（格式 f"{主thread_id}:tag"），不是 done 事件的主thread_id
    # （真实联调排查出的坑，见 实战记录.md 当天条目）。
    thread_id: str
    external_id: str
    interrupt_id: str
    confirmed_add_tag_ids: list[str] = []
    confirmed_remove_tag_ids: list[str] = []


@app.post("/api/confirm_tags")
async def api_confirm_tags(payload: ConfirmTagsRequest, request: Request):
    user = _require_login(request)
    try:
        result = await client.confirm_tags(
            thread_id=payload.thread_id,
            follow_user_id=user["user_id"],
            external_id=payload.external_id,
            interrupt_id=payload.interrupt_id,
            confirmed_add_tag_ids=payload.confirmed_add_tag_ids,
            confirmed_remove_tag_ids=payload.confirmed_remove_tag_ids,
        )
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"success": True, "data": result}


# ---------------------------------------------------------------------------
# F02/F03 回复建议（ 接入）
#
# 与画像/标签的关键区别：kam_agent 端没有interrupt，一次POST请求-响应即
# 完整交互，这里也就不需要 SSE 转发逻辑（_relay_xxx_events），一个路由
# 函数直接调用 client 方法、原样包一层 {success, data} 返回即可。
# ---------------------------------------------------------------------------


class ChatSuggestionRequest(BaseModel):
    external_id: str


@app.post("/api/generate_chat_suggestion")
async def api_generate_chat_suggestion(payload: ChatSuggestionRequest, request: Request):
    """F02.1 销售聊天回复建议。"""
    user = _require_login(request)
    try:
        result = await client.generate_chat_suggestion(
            follow_user_id=user["user_id"], external_id=payload.external_id
        )
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"success": True, "data": result}


@app.post("/api/generate_kf_chat_suggestion")
async def api_generate_kf_chat_suggestion(payload: ChatSuggestionRequest, request: Request):
    """F02.2 客服聊天回复建议。"""
    user = _require_login(request)
    try:
        result = await client.generate_kf_chat_suggestion(
            follow_user_id=user["user_id"], external_id=payload.external_id
        )
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"success": True, "data": result}


# ---------------------------------------------------------------------------
# F16 知识库问答（ 从 kam_sidebar/src/ 平行实现移植接回真正入口）
#
# 起因：`main.py`此前一直没有这两个接口——"F16/F17 UI补齐"那次
# 工作实际上落进了`kam_sidebar/src/`这个从未真正对外服务的平行实现里
# （同一类问题此前`kam_admin`/`kam_client`踩过一次），一直没人发现，直到
# 逐条核对main.py路由才确认。业务逻辑（kam_agent那边的子图/
# prompt）本身没问题，这里只是把已经验证过的调用方式接回真正在跑的入口，
# 不是重新设计。与F02/F03同一种"一次POST请求-响应"结构（kam_agent端没有
# interrupt，不需要SSE）。
# ---------------------------------------------------------------------------


# ：F16不再要求前端传external_id——查证过kam_agent主图的
# load_data节点对不存在的客户已有优雅兜底（external_user查不到时
# union_id/orders自动给空值，不会崩），且kam_agent自己的
# 知识库问答实验测试脚本一直是拿一个不存在的假external_id在跑、
# 验证通过，说明这个字段对F16只是接口层面的必填校验，不是真实功能
# 依赖。用固定占位值代替，只改sidebar这一层，kam_agent的接口契约和
# 主图逻辑都没有动。
_KB_NO_CUSTOMER_EXTERNAL_ID = "kb_only_no_customer"


class KbAnswerRequest(BaseModel):
    question: str


@app.post("/api/kb_answer")
async def api_kb_answer(payload: KbAnswerRequest, request: Request):
    """F16 知识库问答。不跟具体客户绑定，见上方 _KB_NO_CUSTOMER_EXTERNAL_ID 说明。"""
    user = _require_login(request)
    try:
        result = await client.generate_kb_answer(
            follow_user_id=user["user_id"],
            external_id=_KB_NO_CUSTOMER_EXTERNAL_ID,
            question=payload.question,
        )
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"success": True, "data": result}


# ---------------------------------------------------------------------------
# F17 跨模块综合推理（ 移植，原因同上）
#
# 与画像/标签同款两段式：先发号拿thread_id，SSE连上后kam_agent才真正执行
# （真实耗时20+秒，避免"POST已跑完、SSE还没连上、事件被漏掉"的竞态）。
# F17事件（plan_ready/step_done/synthesis_done等）本身已带完整内容，不像
# profile_field_update那样需要补充follow_user_id/external_id再转发。
# ---------------------------------------------------------------------------


class GenerateReasoningRequest(BaseModel):
    external_id: str
    question: str


@app.post("/api/generate_reasoning")
async def api_generate_reasoning(payload: GenerateReasoningRequest, request: Request):
    user = _require_login(request)
    try:
        thread_id = await client.generate_reasoning(
            follow_user_id=user["user_id"],
            external_id=payload.external_id,
            question=payload.question,
        )
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"success": True, "data": {"thread_id": thread_id}}


async def _relay_reasoning_events(thread_id: str, follow_user_id: str, external_id: str, question: str):
    try:
        async for evt in client.stream_reasoning_events(thread_id, follow_user_id, external_id, question):
            yield _format_sse(evt["event"], evt["data"])
    except KamAgentAPIError as exc:
        yield _format_sse("error", {"message": str(exc)})


@app.get("/api/stream_reasoning/{thread_id}")
async def api_stream_reasoning(thread_id: str, external_id: str, question: str, request: Request):
    user = _require_login(request)
    return StreamingResponse(
        _relay_reasoning_events(thread_id, user["user_id"], external_id, question),
        media_type="text/event-stream",
    )


@app.get("/api/customers")
async def api_customers(request: Request):
    """返回登录顾问自己的客户列表，不接受前端传入其他顾问ID。"""
    user = _require_login(request)
    try:
        result = await client.get_customers(user["user_id"])
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"success": True, "data": result}


@app.get("/api/chat_history")
async def api_chat_history(external_id: str, channel: str, request: Request):
    """转发指定客户和场景的聊天记录查询。"""
    user = _require_login(request)
    try:
        result = await client.get_chat_history(user["user_id"], external_id, channel)
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"success": True, "data": result}


@app.get("/api/customer_profile")
async def api_customer_profile(external_id: str, request: Request):
    """只读查看客户已确认/在途的画像内容，不触发生成（"沟通记录"tab右侧
    客户信息卡片"查看画像详情"按钮用）。跟
    /api/generate_profile是完全独立的两条路径：这里只读kam_agent已有的
    /store/get通用接口，不会调LLM、不会进入interrupt确认流程。"""
    user = _require_login(request)
    try:
        result = await client.get_customer_profile(follow_user_id=user["user_id"], external_id=external_id)
    except KamAgentAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"success": True, "data": result}


# ---------------------------------------------------------------------------
# 健康检查 + 页面
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def api_health():
    return {"status": "healthy"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = _get_current_user(request)
    if user is None:
        return RedirectResponse(url="/static/login.html", status_code=303)
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


# 注意：挂载在 /static 子路径而不是根路径 "/"——如果挂载在根路径，
# StaticFiles 会拦截所有请求（包括上面这个 "/" 路由函数本身要处理的登录跳转
# 判断逻辑），FastAPI 是按注册顺序 + 路径前缀匹配的，根路径挂载会让
# index() 这个函数永远执行不到。
app.mount("/static", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8002)
