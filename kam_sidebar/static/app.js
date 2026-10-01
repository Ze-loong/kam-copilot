/**
 * kam_sidebar 主页面逻辑。
 *
 * 对应 06号设计文档 决策3（逐字段确认交互怎么在前端管理状态）：
 *   fieldsState 是一个以 field 名为 key 的对象，SSE 每收到一条
 *   profile_field_update 事件就往里面写一条（含这个字段自己的
 *   thread_id/interrupt_id）。每张卡片的确认按钮绑定的是"这个字段自己的"
 *   thread_id/interrupt_id，不是全局共用一份——这是本模块唯一的交互难点，
 *   因为每个字段是独立的子图执行实例（05号文档已确认），传错就会导致
 *   /tasks/confirm 找不到对应的 interrupt。
 *
 *   recreate 之后，响应体带回新草稿，这里用 new_interrupt_id 覆盖旧的
 *   interrupt_id（06号文档决策3特别提醒的"容易漏改的点"）——如果这里
 *   忘了更新，下一次点 ok/discard 会用旧的、已经失效的 interrupt_id。
 */

let currentUser = null;
let currentExternalId = null;
const fieldsState = {}; // { [field]: {value, confidence, source, status, thread_id, interrupt_id} }

// ---------------------------------------------------------------------------
// 初始化：拿当前登录人信息
// ---------------------------------------------------------------------------

async function loadCurrentUser() {
  const resp = await fetch("/api/me");
  if (resp.status === 401) {
    window.location.href = "/static/login.html";
    return;
  }
  const body = await resp.json();
  currentUser = body.data;
  document.getElementById("user-info").textContent =
    `${currentUser.name}（${currentUser.role} / ${currentUser.region}）`;
}

document.getElementById("logout-btn").addEventListener("click", async () => {
  await fetch("/logout", { method: "POST" });
  window.location.href = "/static/login.html";
});

// ---------------------------------------------------------------------------
// Tab 切换（回复建议/标签/日程是占位；"沟通记录"是从独立的
// /chat 页面合并进来的第5个tab，逻辑上单独处理——它不属于 #panel-standard
// 里那4个互斥的标准tab，而是整个 #panel-standard 和 #tab-chat 两个大区块
// 之间切换，#tab-chat 首次进入时才懒加载数据（跟其余4个tab一样不预加载）。
// ---------------------------------------------------------------------------

const STANDARD_TABS = ["profile", "reply", "tag", "schedule", "kb", "reasoning"];
let chatTabInitialized = false;

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    const isChat = tab === "chat";

    document.getElementById("panel-standard").style.display = isChat ? "none" : "block";
    document.getElementById("tab-chat").style.display = isChat ? "grid" : "none";

    if (isChat) {
      if (!chatTabInitialized) {
        chatTabInitialized = true;
        window.initChatTab();
      }
    } else {
      STANDARD_TABS.forEach((name) => {
        document.getElementById(`tab-${name}`).style.display = name === tab ? "block" : "none";
      });
    }
  });
});

// ---------------------------------------------------------------------------
// 发起生成画像
// ---------------------------------------------------------------------------

document.getElementById("generate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const externalId = document.getElementById("external_id").value.trim();
  if (!externalId) return;

  currentExternalId = externalId;
  Object.keys(fieldsState).forEach((k) => delete fieldsState[k]);
  document.getElementById("field-list").innerHTML = "";
  document.getElementById("empty-hint").textContent = "正在生成画像草稿…";
  document.getElementById("empty-hint").style.display = "block";

  const genBtn = document.getElementById("generate-btn");
  genBtn.disabled = true;

  try {
    const resp = await fetch("/api/generate_profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ external_id: externalId }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || "生成失败");
    }
    const body = await resp.json();
    const threadId = body.data.thread_id;
    startStream(threadId, externalId);
  } catch (err) {
    document.getElementById("empty-hint").textContent = `出错了：${err.message}`;
    genBtn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// SSE：接收字段草稿事件
// ---------------------------------------------------------------------------

function startStream(threadId, externalId) {
  const url = `/api/stream/${threadId}?external_id=${encodeURIComponent(externalId)}`;
  const es = new EventSource(url);

  es.addEventListener("profile_field_update", (evt) => {
    const data = JSON.parse(evt.data);
    fieldsState[data.field] = {
      value: data.value,
      confidence: data.confidence,
      source: data.source,
      status: data.status,
      thread_id: data.thread_id, // 这个字段自己的子 thread_id，不是主 thread_id
      interrupt_id: data.interrupt_id,
    };
    renderFieldCards();
  });

  es.addEventListener("done", () => {
    es.close();
    document.getElementById("generate-btn").disabled = false;
    if (Object.keys(fieldsState).length === 0) {
      document.getElementById("empty-hint").textContent = "没有生成出任何画像字段";
    } else {
      document.getElementById("empty-hint").style.display = "none";
    }
  });

  es.addEventListener("error", (evt) => {
    // 后端 _relay_profile_events 在捕获到 KamAgentAPIError 时会发一条
    // 自定义 "error" 事件；EventSource 原生的连接级错误也会走这里，
    // 两种情况都提示用户、停止转圈，不区分对用户没有意义。
    document.getElementById("empty-hint").style.display = "block";
    document.getElementById("empty-hint").textContent = "连接出错，请重试";
    document.getElementById("generate-btn").disabled = false;
    es.close();
  });
}

// ---------------------------------------------------------------------------
// 渲染字段卡片 + 绑定确认按钮
// ---------------------------------------------------------------------------

const STATUS_LABEL = {
  draft: "待确认",
  need_verify: "需核实",
  confirmed: "已确认",
  discarded: "已放弃",
  need_regenerate: "待重新生成",
};

// 画像字段中文名（口径见 设计文档/12-数据字典.md 第二节）；未知字段回退显示原 key
const FIELD_LABEL = {
  company_name: "所属企业",
  company_scale: "企业规模",
  production_status: "主营产品与产线现状",
  project_stage: "技改项目阶段",
  pain_points: "核心痛点",
  tech_goal: "技改目标",
  budget_range: "预算区间",
  decision_makers: "决策链",
  decision_style: "决策风格",
  price_sensitivity: "价格敏感度",
  reply_time: "活跃沟通时段",
  competitor_info: "竞品接触情况",
};

function renderFieldCards() {
  const listEl = document.getElementById("field-list");
  listEl.innerHTML = "";

  for (const [field, item] of Object.entries(fieldsState)) {
    const card = document.createElement("div");
    card.className = "field-card";

    const isTerminal = item.status === "confirmed" || item.status === "discarded";

    const status = Object.hasOwn(STATUS_LABEL, item.status) ? item.status : "draft";
    card.innerHTML = `
      <div class="field-name">${escapeHtml(FIELD_LABEL[field] || field)}
        <span class="status-badge status-${status}">${escapeHtml(STATUS_LABEL[item.status] || item.status)}</span>
      </div>
      <div class="field-value">${escapeHtml(item.value ?? "（空）")}</div>
      <div class="field-meta">置信度 ${escapeHtml(item.confidence ?? "-")} · 来源：${escapeHtml(item.source ?? "-")}</div>
      <div class="field-actions">
        <button class="ok" ${isTerminal ? "disabled" : ""}>确认</button>
        <button class="discard" ${isTerminal ? "disabled" : ""}>放弃</button>
        <button class="recreate" ${isTerminal ? "disabled" : ""}>重新生成</button>
      </div>
    `;

    // 每个按钮的点击事件用闭包绑定"这个字段自己此刻的" thread_id/interrupt_id，
    // 而不是从某个全局变量读——因为 recreate 之后 fieldsState[field] 会被替换成
    // 新的 thread_id/interrupt_id，下次重新渲染时闭包会自然拿到最新值。
    const [okBtn, discardBtn, recreateBtn] = card.querySelectorAll(".field-actions button");
    okBtn.addEventListener("click", () => confirmField(field, "ok"));
    discardBtn.addEventListener("click", () => confirmField(field, "discard"));
    recreateBtn.addEventListener("click", () => confirmField(field, "recreate"));

    listEl.appendChild(card);
  }
}

async function confirmField(field, action) {
  const item = fieldsState[field];
  if (!item) return;

  try {
    const resp = await fetch("/api/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        thread_id: item.thread_id,
        external_id: currentExternalId,
        field,
        interrupt_id: item.interrupt_id,
        action,
      }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || "确认失败");
    }
    const body = await resp.json();
    const result = body.data;

    if (action === "recreate") {
      // recreate 响应体直接带回新草稿（02号文档5.1.3节），原地更新这张卡片。
      // 关键：interrupt_id 必须换成 new_interrupt_id，否则下次点击会用旧的、
      // 已经失效的 interrupt_id 去调 /tasks/confirm（06号文档决策3提醒的坑）。
      fieldsState[field] = {
        ...fieldsState[field],
        value: result.new_value,
        confidence: result.new_confidence,
        source: result.new_source,
        status: result.status, // recreate 后固定是 "draft"
        interrupt_id: result.new_interrupt_id,
        // thread_id 不变：同一个字段的子图执行实例没有变，只是子图内部又走了一轮
      };
    } else {
      // ok / discard：终态，只需要更新 status
      fieldsState[field] = { ...fieldsState[field], status: result.status };
    }

    renderFieldCards();
  } catch (err) {
    alert(`操作失败：${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// F04标签推荐（）
//
// 与画像流程的关键交互差异：画像是"逐字段渲染卡片、每张卡片各自确认"，
// 标签是"整批渲染成一个勾选列表、一次性提交"——对应08号设计文档"整批
// 一次interrupt"的架构判断，前端交互也要跟着变成批量勾选而不是逐条按钮。
// tagState 只需要存"这一批推荐的原始数据 + 当前的thread_id/interrupt_id"，
// 不需要像 fieldsState 那样按字段/按条目分别管理各自的确认状态。
// ---------------------------------------------------------------------------

let currentTagExternalId = null;
let tagState = null; // { thread_id, interrupt_id, add_recommendations, remove_recommendations }

document.getElementById("generate-tags-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const externalId = document.getElementById("tag_external_id").value.trim();
  if (!externalId) return;

  currentTagExternalId = externalId;
  tagState = null;
  document.getElementById("tag-result").innerHTML = "";
  document.getElementById("tag-empty-hint").textContent = "正在生成标签推荐…";
  document.getElementById("tag-empty-hint").style.display = "block";

  const genBtn = document.getElementById("generate-tags-btn");
  genBtn.disabled = true;

  try {
    const resp = await fetch("/api/generate_tags", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ external_id: externalId }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || "生成失败");
    }
    const body = await resp.json();
    const threadId = body.data.thread_id;
    startTagStream(threadId, externalId);
  } catch (err) {
    document.getElementById("tag-empty-hint").textContent = `出错了：${err.message}`;
    genBtn.disabled = false;
  }
});

function startTagStream(threadId, externalId) {
  const url = `/api/stream_tags/${threadId}?external_id=${encodeURIComponent(externalId)}`;
  const es = new EventSource(url);

  es.addEventListener("tag_batch_update", (evt) => {
    const data = JSON.parse(evt.data);
    // 注意：data.thread_id 是标签子图自己的 thread_id（格式
    // "主thread_id:tag"），不是这里发起SSE用的主 threadId——提交确认时
    // 必须用 data.thread_id，这是真实联调排查出的坑。
    tagState = {
      thread_id: data.thread_id,
      interrupt_id: data.interrupt_id,
      add_recommendations: data.add_recommendations || [],
      remove_recommendations: data.remove_recommendations || [],
    };
    renderTagResult();
  });

  es.addEventListener("done", () => {
    es.close();
    document.getElementById("generate-tags-btn").disabled = false;

    // ：confirm_tag_batch 无条件调用 interrupt()，
    // 即便 add_recommendations/remove_recommendations 都是空数组也会照常
    // 发一条 tag_batch_update 事件——所以"收到过 tag_batch_update" 不等于
    // "有真实推荐内容"，不能只用 `!tagState` 判断空状态（tagState 在两个
    // 数组都为空时依然是一个非null对象，`!tagState` 恒为 false）。此前
    // 这里错误地判断成"有tagState就隐藏提示"，导致 renderTagResult() 刚
    // 正确显示的"没有生成出任何标签推荐"提示，被这里立刻覆盖隐藏。
    // 改成直接检查两个推荐数组的实际长度，不看 tagState 是否为 null。
    const hasRecommendations =
      tagState &&
      (tagState.add_recommendations.length > 0 || tagState.remove_recommendations.length > 0);

    if (!hasRecommendations) {
      document.getElementById("tag-empty-hint").style.display = "block";
      document.getElementById("tag-empty-hint").textContent =
        "没有生成出任何标签推荐（宁缺毋滥，可能是证据不够明确）";
    } else {
      document.getElementById("tag-empty-hint").style.display = "none";
    }
  });

  es.addEventListener("error", (evt) => {
    // ：这个error分支曾出现过一次未稳定复现的"连接出错"，
    // 不确定是真实网络问题还是EventSource在done事件后的正常关闭时序
    // 产生的误报（浏览器有已知行为：服务端结束流的时机如果和es.close()
    // 有竞争，可能会先触发一次error）。这里只加console.log留诊断线索，
    // 不改变原有行为——没有实证前不去猜测修一个不确定复现的问题。
    console.log("[tag SSE error]", { readyState: es.readyState, event: evt });
    document.getElementById("tag-empty-hint").style.display = "block";
    document.getElementById("tag-empty-hint").textContent = "连接出错，请重试";
    document.getElementById("generate-tags-btn").disabled = false;
    es.close();
  });
}

function renderTagResult() {
  const container = document.getElementById("tag-result");
  container.innerHTML = "";

  if (!tagState) return;

  const { add_recommendations, remove_recommendations } = tagState;

  if (add_recommendations.length === 0 && remove_recommendations.length === 0) {
    document.getElementById("tag-empty-hint").style.display = "block";
    document.getElementById("tag-empty-hint").textContent = "没有生成出任何标签推荐（宁缺毋滥，可能是证据不够明确）";
    return;
  }
  document.getElementById("tag-empty-hint").style.display = "none";

  const card = document.createElement("div");
  card.className = "card";

  const addSectionHtml = add_recommendations.length
    ? `<div class="field-name">建议新增</div>` +
      add_recommendations
        .map(
          (item) => `
        <label class="tag-checkbox-row">
          <input type="checkbox" class="tag-add-checkbox" value="${escapeHtml(item.tag_id)}" checked />
          <span>${escapeHtml(item.tag_name)}</span>
          <div class="field-meta">${escapeHtml(item.reason)}</div>
        </label>`
        )
        .join("")
    : "";

  const removeSectionHtml = remove_recommendations.length
    ? `<div class="field-name" style="margin-top:12px">建议移除</div>` +
      remove_recommendations
        .map(
          (item) => `
        <label class="tag-checkbox-row">
          <input type="checkbox" class="tag-remove-checkbox" value="${escapeHtml(item.tag_id)}" checked />
          <span>${escapeHtml(item.tag_name)}</span>
          <div class="field-meta">${escapeHtml(item.reason)}</div>
        </label>`
        )
        .join("")
    : "";

  card.innerHTML =
    addSectionHtml +
    removeSectionHtml +
    `<div class="field-actions" style="margin-top:12px">
      <button class="ok" id="submit-tags-btn">提交确认</button>
    </div>`;

  container.appendChild(card);

  document.getElementById("submit-tags-btn").addEventListener("click", submitTagConfirmation);
}

async function submitTagConfirmation() {
  if (!tagState) return;

  // 前端只负责"用户选了什么"，把勾选框状态整理成两个id列表交给后端；
  // 后端（confirm_tag_batch的_validate_confirmed_ids）负责"这个选择能不能
  // 执行"的二次校验，这是前后端的职责划分（见 设计文档/08-F04标签推荐设计文档.md
  //  "确认机制的职责划分"条目）。
  const confirmedAddTagIds = Array.from(
    document.querySelectorAll(".tag-add-checkbox:checked")
  ).map((el) => el.value);
  const confirmedRemoveTagIds = Array.from(
    document.querySelectorAll(".tag-remove-checkbox:checked")
  ).map((el) => el.value);

  const submitBtn = document.getElementById("submit-tags-btn");
  submitBtn.disabled = true;

  try {
    const resp = await fetch("/api/confirm_tags", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        thread_id: tagState.thread_id,
        external_id: currentTagExternalId,
        interrupt_id: tagState.interrupt_id,
        confirmed_add_tag_ids: confirmedAddTagIds,
        confirmed_remove_tag_ids: confirmedRemoveTagIds,
      }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || "确认失败");
    }
    const body = await resp.json();
    const result = body.data;

    document.getElementById("tag-result").innerHTML = "";
    document.getElementById("tag-empty-hint").style.display = "block";
    document.getElementById("tag-empty-hint").textContent =
      `已确认：新增${result.confirmed_add_tag_ids.length}个、移除${result.confirmed_remove_tag_ids.length}个`;
    tagState = null;
  } catch (err) {
    alert(`提交失败：${err.message}`);
    submitBtn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// F02/F03 回复建议（）
//
// 与画像(fieldsState)/标签(tagState)两套流程的关键区别：没有SSE、没有
// interrupt确认闭环，一次POST请求-响应就是完整交互——提交表单后直接拿到
// {suggestion_text, reasoning}，渲染出来即结束，不需要维护任何"待确认"
// 状态、不需要EventSource、不需要确认/勾选按钮（09号设计文档判断点4：
// 结果仅展示，最终发送行为由用户手动操作完成，系统不介入）。
// ---------------------------------------------------------------------------

document.getElementById("generate-reply-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const externalId = document.getElementById("reply_external_id").value.trim();
  const scene = document.getElementById("reply_scene").value; // "sales" / "kf"
  if (!externalId) return;

  const resultEl = document.getElementById("reply-result");
  const hintEl = document.getElementById("reply-empty-hint");
  const genBtn = document.getElementById("generate-reply-btn");

  resultEl.innerHTML = "";
  hintEl.style.display = "block";
  hintEl.textContent = "正在生成回复建议…";
  genBtn.disabled = true;

  // 场景区分接口路径，不靠后端猜（与09号设计文档判断点3一致：意图由
  // 接口层显式区分，销售/客服菜单各自调用不同的URL）。
  const endpoint =
    scene === "kf" ? "/api/generate_kf_chat_suggestion" : "/api/generate_chat_suggestion";

  try {
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ external_id: externalId }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || "生成失败");
    }
    const body = await resp.json();
    renderReplyResult(body.data);
  } catch (err) {
    hintEl.textContent = `出错了：${err.message}`;
  } finally {
    genBtn.disabled = false;
  }
});

function renderReplyResult(data) {
  const resultEl = document.getElementById("reply-result");
  const hintEl = document.getElementById("reply-empty-hint");

  if (!data || !data.suggestion_text) {
    hintEl.style.display = "block";
    hintEl.textContent = "没有生成出回复建议";
    return;
  }
  hintEl.style.display = "none";

  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <div class="field-name">回复建议</div>
    <div class="field-value">${escapeHtml(data.suggestion_text)}</div>
    <div class="field-meta">推理说明：${escapeHtml(data.reasoning)}</div>
  `;
  resultEl.innerHTML = "";
  resultEl.appendChild(card);
}

// ---------------------------------------------------------------------------
// F16 知识库问答 / F17 跨模块综合推理（ 移植自 kam_sidebar/src/
// 平行实现，详见任务清单.md kam_sidebar小节）
//
// escapeHtml()：所有用 innerHTML 渲染的用户、数据库及 LLM 文本
// 都要先转义，避免内容中的 HTML 标签被浏览器执行。
// ---------------------------------------------------------------------------

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

// ==================== F16：一次POST请求-响应，跟F02/F03同结构 ====================

document.getElementById("generate-kb-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = document.getElementById("kb_question").value.trim();
  if (!question) return;

  const resultEl = document.getElementById("kb-result");
  const hintEl = document.getElementById("kb-empty-hint");
  const genBtn = document.getElementById("generate-kb-btn");

  resultEl.innerHTML = "";
  hintEl.style.display = "block";
  hintEl.textContent = "正在检索资料并生成回答…";
  genBtn.disabled = true;

  try {
    const resp = await fetch("/api/kb_answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || "生成失败");
    }
    const body = await resp.json();
    renderKbResult(body.data);
  } catch (err) {
    hintEl.style.display = "block";
    hintEl.textContent = `出错了：${err.message}`;
  } finally {
    genBtn.disabled = false;
  }
});

function renderKbResult(data) {
  const resultEl = document.getElementById("kb-result");
  const hintEl = document.getElementById("kb-empty-hint");

  if (!data || !data.answer_text) {
    hintEl.style.display = "block";
    hintEl.textContent = "没有生成出回答";
    return;
  }
  hintEl.style.display = "none";

  const sources = (data.cited_sources || []).length
    ? data.cited_sources.map(escapeHtml).join("、")
    : "无（兜底话术，未命中知识库）";

  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <div class="field-name">回答</div>
    <div class="field-value">${escapeHtml(data.answer_text)}</div>
    <div class="field-meta">引用来源：${sources}</div>
  `;
  resultEl.innerHTML = "";
  resultEl.appendChild(card);
}

// ==================== F17：两段式，SSE实时步骤推送 ====================
// kam_agent那边用LangGraph官方stream_mode="custom"，节点真正算完那一刻
// 才推一条消息（流式推送实验验证过消息间有真实秒级间隔，不是
// 攒完一次性倒出来），所以这里要维护"计划生成中/第几步/综合分析中"的
// 实时状态展示，不是等一个res.json()。

document.getElementById("generate-reasoning-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const externalId = document.getElementById("reasoning_external_id").value.trim();
  const question = document.getElementById("reasoning_question").value.trim();
  if (!externalId || !question) return;

  const hintEl = document.getElementById("reasoning-empty-hint");
  const genBtn = document.getElementById("generate-reasoning-btn");

  document.getElementById("reasoning-result").innerHTML = "";
  hintEl.style.display = "block";
  hintEl.textContent = "正在发起推理任务…";
  genBtn.disabled = true;

  try {
    const resp = await fetch("/api/generate_reasoning", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ external_id: externalId, question: question }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || "发起失败");
    }
    const body = await resp.json();
    startReasoningStream(body.data.thread_id, externalId, question);
  } catch (err) {
    hintEl.style.display = "block";
    hintEl.textContent = `出错了：${err.message}`;
    genBtn.disabled = false;
  }
});

function startReasoningStream(threadId, externalId, question) {
  document.getElementById("reasoning-empty-hint").style.display = "none";

  const resultEl = document.getElementById("reasoning-result");
  resultEl.innerHTML = `
    <div class="card">
      <div class="field-name" id="reasoning-status">正在生成查询计划…</div>
      <ul id="reasoning-plan" style="display:none"></ul>
      <ul id="reasoning-steps"></ul>
      <div id="reasoning-final"></div>
    </div>
  `;
  const statusEl = document.getElementById("reasoning-status");
  const genBtn = document.getElementById("generate-reasoning-btn");

  const url = `/api/stream_reasoning/${threadId}?external_id=${encodeURIComponent(externalId)}&question=${encodeURIComponent(question)}`;
  const es = new EventSource(url);

  es.addEventListener("planning_start", () => {
    statusEl.textContent = "正在生成查询计划…";
  });

  es.addEventListener("plan_ready", (evt) => {
    const { plan } = JSON.parse(evt.data);
    statusEl.textContent = "计划已生成，开始逐步执行…";
    const planEl = document.getElementById("reasoning-plan");
    planEl.style.display = "block";
    planEl.innerHTML =
      '<li class="field-meta"><strong>查询计划：</strong></li>' +
      plan.map((s) => `<li class="field-meta">[${escapeHtml(s.tool)}] ${escapeHtml(s.reason)}</li>`).join("");
  });

  es.addEventListener("step_start", (evt) => {
    const step = JSON.parse(evt.data);
    statusEl.textContent = step.display_text;
    const stepsEl = document.getElementById("reasoning-steps");
    let li = document.getElementById(`reasoning-step-${step.step_index}`);
    if (!li) {
      li = document.createElement("li");
      li.id = `reasoning-step-${step.step_index}`;
      li.className = "field-meta";
      stepsEl.appendChild(li);
    }
    li.textContent = step.display_text;
  });

  es.addEventListener("step_done", (evt) => {
    const { step_result: stepResult } = JSON.parse(evt.data);
    const li = document.getElementById(`reasoning-step-${stepResult.step_index}`);
    if (li) li.textContent = stepResult.display_text;
  });

  es.addEventListener("synthesizing_start", () => {
    statusEl.textContent = "全部信息已查询完毕，正在综合分析生成建议（这一步通常耗时最长）…";
  });

  es.addEventListener("synthesis_done", (evt) => {
    const { final_suggestion: finalSuggestion, confidence_note: confidenceNote } = JSON.parse(evt.data);
    statusEl.textContent = "推理完成";
    const confidenceHtml = confidenceNote
      ? `<div class="field-meta">⚠️ 不确定性标注：${escapeHtml(confidenceNote)}</div>`
      : `<div class="field-meta">信息充分，无不确定性标注。</div>`;
    document.getElementById("reasoning-final").innerHTML = `
      <div class="field-value">${escapeHtml(finalSuggestion)}</div>
      ${confidenceHtml}
    `;
  });

  es.addEventListener("kb_fallback", (evt) => {
    const { answer_text: answerText, cited_sources: citedSources } = JSON.parse(evt.data);
    statusEl.textContent = "全局关停开关当前处于关闭状态，已自动退回F16知识库简化模式：";
    document.getElementById("reasoning-final").innerHTML = `
      <div class="field-value">${escapeHtml(answerText)}</div>
      <div class="field-meta">引用来源：${(citedSources || []).map(escapeHtml).join("、") || "无"}</div>
    `;
  });

  es.addEventListener("done", () => {
    es.close();
    genBtn.disabled = false;
  });

  es.addEventListener("error", (evt) => {
    // 与tag SSE的error分支同一条既有结论：后端捕获KamAgentAPIError会发
    // 自定义"error"事件，EventSource原生连接级错误也走这里，两种情况都
    // 提示用户、停止转圈，不需要区分。
    console.log("[reasoning SSE error]", { readyState: es.readyState, event: evt });
    statusEl.textContent = "连接出错，请重试";
    es.close();
    genBtn.disabled = false;
  });
}

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------

loadCurrentUser();
