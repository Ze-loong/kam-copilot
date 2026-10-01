const state={customers:[],selected:null,channel:"wxqy_msg"};
const $=id=>document.getElementById(id);
function escapeHtml(value){return String(value??"").replace(/[&<>'"]/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char])}
function displayName(customer){return customer.remark_name||customer.name||customer.external_id}
function initial(customer){return displayName(customer).trim().slice(0,1)||"客"}
async function fetchJson(url,options){const response=await fetch(url,options);const payload=await response.json().catch(()=>({}));if(!response.ok)throw new Error(payload.detail||`请求失败（${response.status}）`);return payload}
// has_profile 徽标（）：mock客户不是全部都预生成了画像
// （生成是顾问按需触发的，见 webapp.py list_customers 的说明），列表里
// 用一个小勾标出哪些已经有画像，不用逐个点开客户画像tab才知道。
function renderCustomers(){const list=$("customer-list");$("customer-count").textContent=state.customers.length;if(!state.customers.length){list.innerHTML='<div class="state-card">当前账号下没有客户</div>';return}list.innerHTML=state.customers.map(customer=>`<button class="customer-item ${state.selected?.external_id===customer.external_id?"active":""}" data-id="${escapeHtml(customer.external_id)}"><span class="avatar">${escapeHtml(initial(customer))}</span><span class="customer-copy"><strong>${escapeHtml(displayName(customer))}${customer.has_profile?'<span class="profile-badge" title="已生成画像">✓</span>':""}</strong><small>${escapeHtml(customer.name||customer.external_id)}</small></span></button>`).join("");list.querySelectorAll(".customer-item").forEach(button=>button.addEventListener("click",()=>selectCustomer(button.dataset.id)))}
// 画像字段状态标签（，配合"查看画像详情"）。命名跟
// app.js的STATUS_LABEL区分开——两个文件都是经典<script>（非module），
// 顶层const在同一全局作用域下重名会直接SyntaxError把整个文件炸掉，
// 不能假设两边随便同名（这个坑是这次顺带发现的，见escapeHtml那条备忘）。
const PROFILE_FIELD_STATUS_LABEL={draft:"待确认",need_verify:"需核实",confirmed:"已确认",discarded:"已放弃",need_regenerate:"待重新生成"};

function renderProfile(customer){const tags=Array.isArray(customer.tags)?customer.tags:[];const profileStatus=customer.has_profile?'<span class="profile-status yes">✓ 已生成画像</span>':'<span class="profile-status no">尚未生成画像</span>';const viewBtn=customer.has_profile?'<div class="field-actions"><button id="view-profile-btn">查看画像详情</button></div>':"";$("profile-card").innerHTML=`<div class="profile-avatar">${escapeHtml(initial(customer))}</div><h3>${escapeHtml(displayName(customer))}</h3><p>${escapeHtml(customer.name||"暂无姓名")}</p>${profileStatus}<div class="info-row"><span>客户ID</span><b>${escapeHtml(customer.external_id)}</b></div><div class="info-row"><span>Union ID</span><b>${escapeHtml(customer.union_id||"—")}</b></div><div class="info-row"><span>客户标签</span><b>${tags.length} 个</b></div><div class="tags">${tags.length?tags.map(tag=>`<span class="tag">${escapeHtml(typeof tag==="string"?tag:(tag.name||tag.tag_name||tag.tag_id||"标签"))}</span>`).join(""):'<span class="tag">暂无标签</span>'}</div>${viewBtn}<div id="profile-detail"></div>`;if(customer.has_profile)$("view-profile-btn").addEventListener("click",()=>viewProfileDetail(customer.external_id))}

// 只读查看已确认画像，不触发重新生成——跟"客户画像"tab点"生成画像"是
// 两条完全独立的路径（那个会真实调LLM+进interrupt确认流程）。点一次就把
// 按钮换成结果，不需要反复点（数据不会在本次会话内变化，除非重新生成）。
async function viewProfileDetail(externalId){const btn=$("view-profile-btn");const detail=$("profile-detail");btn.disabled=true;btn.textContent="加载中...";detail.innerHTML="";try{const payload=await fetchJson(`/api/customer_profile?external_id=${encodeURIComponent(externalId)}`);renderProfileDetail(payload.data);btn.remove()}catch(error){detail.innerHTML=`<div class="empty-state compact error">${escapeHtml(error.message)}</div>`;btn.disabled=false;btn.textContent="查看画像详情"}}

function renderProfileDetail(profile){const detail=$("profile-detail");const items=profile&&profile.profile_items?profile.profile_items:{};const fields=Object.entries(items);if(!fields.length){detail.innerHTML='<div class="empty-state compact">还没有画像字段数据</div>';return}detail.innerHTML=fields.map(([field,item])=>`<div class="field-card"><div class="field-name">${escapeHtml(field)}<span class="status-badge status-${escapeHtml(item.status)}">${escapeHtml(PROFILE_FIELD_STATUS_LABEL[item.status]||item.status)}</span></div><div class="field-value">${escapeHtml(item.value??"（空）")}</div><div class="field-meta">置信度 ${item.confidence??"-"} · 来源：${escapeHtml(item.source??"-")}</div></div>`).join("")}
function formatTime(value){if(!value)return "时间未知";return String(value).replace("T"," ").slice(0,16)}
function renderMessages(messages){const list=$("message-list");if(!messages.length){list.innerHTML='<div class="empty-state"><strong>这个渠道暂无聊天记录</strong><span>可切换到另一个渠道查看</span></div>';return}list.innerHTML=messages.map(message=>`<div class="message-row ${message.sender}"><div class="message-wrap"><div class="message-meta">${message.sender==="advisor"?"顾问/客服":"客户"} · ${escapeHtml(formatTime(message.msg_time))}</div><div class="bubble">${escapeHtml(message.content)}</div></div></div>`).join("");list.scrollTop=list.scrollHeight}
async function loadHistory(){if(!state.selected)return;const list=$("message-list");list.innerHTML='<div class="empty-state">正在加载聊天记录...</div>';$("suggestion-card").hidden=true;try{const query=new URLSearchParams({external_id:state.selected.external_id,channel:state.channel});const payload=await fetchJson(`/api/chat_history?${query}`);renderMessages(payload.data.messages||[])}catch(error){list.innerHTML=`<div class="empty-state error">${escapeHtml(error.message)}</div>`}}
async function selectCustomer(externalId){state.selected=state.customers.find(item=>item.external_id===externalId);renderCustomers();renderProfile(state.selected);$("conversation-title").textContent=displayName(state.selected);$("conversation-subtitle").textContent=`${state.selected.name||"客户"} · ${state.selected.external_id}`;$("suggest-btn").disabled=false;await loadHistory()}
async function generateSuggestion(){if(!state.selected)return;const button=$("suggest-btn");const card=$("suggestion-card");button.disabled=true;button.textContent="AI 正在分析历史对话...";card.hidden=true;try{const endpoint=state.channel==="wxqy_msg"?"/api/generate_chat_suggestion":"/api/generate_kf_chat_suggestion";const payload=await fetchJson(endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({external_id:state.selected.external_id})});const result=payload.data;card.innerHTML=`<div class="suggestion-label">AI REPLY SUGGESTION · 尚未发送</div><div class="suggestion-text">${escapeHtml(result.suggestion_text)}</div><p class="reasoning"><strong>建议依据：</strong>${escapeHtml(result.reasoning)}</p>`;card.hidden=false}catch(error){card.innerHTML=`<div class="error">${escapeHtml(error.message)}</div>`;card.hidden=false}finally{button.disabled=false;button.innerHTML="<span>✦</span> 重新生成 AI 话术建议"}}
// ：user-info/logout-btn 已经并入 index.html 共用的顶栏，
// 由 app.js 的 loadCurrentUser()/logout 监听统一处理，这里不再重复拿
// /api/me、也不再重复绑定 logout-btn（同一个按钮绑两次监听虽不会报错，
// 但没必要，容易让人以为这里还是独立页面）。
async function init(){try{const customers=await fetchJson("/api/customers");state.customers=customers.data.customers||[];renderCustomers();if(state.customers.length)await selectCustomer(state.customers[0].external_id)}catch(error){$("customer-list").innerHTML=`<div class="state-card error">${escapeHtml(error.message)}</div>`}}

// "沟通记录"tab 首次被点击时，由 app.js 的 tab 切换逻辑调用一次，
// 不在脚本加载时立刻执行——跟其余4个tab一样，不访问就不预先请求数据。
window.initChatTab=function(){
  document.querySelectorAll(".channel-btn").forEach(button=>button.addEventListener("click",async()=>{document.querySelectorAll(".channel-btn").forEach(item=>item.classList.remove("active"));button.classList.add("active");state.channel=button.dataset.channel;await loadHistory()}));
  $("suggest-btn").addEventListener("click",generateSuggestion);
  init();
};
