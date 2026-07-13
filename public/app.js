// app.js — مجلس أمد المالي × محاكاة تطبيق الإنماء
// ══════════════════════════════════════════════════════════════════
// - التنقل بين الشاشتين (البنك ↔ المجلس) عبر تبديل كلاس .active، بدون
//   أي إعادة تحميل للصفحة.
// - بيانات رغد المالية ثابتة (Mock) من الشاشة الأولى، وتُحقن تلقائياً
//   ضمن كائن financial_context مع كل طلب لـ /api/council — لا حاجة
//   لأي وكيل أن يسأل عن الدخل أو الرصيد.
// - متوافق حرفياً مع أحداث SSE من server.py: evaluating, agent_start,
//   token, agent_done, agent_skipped, council_done, error, tool_call.
// ══════════════════════════════════════════════════════════════════

// ─── بيانات رغد المالية (من شاشة تطبيق الإنماء) ───────────────
// دخلها الشهري (الراتب المودع)، رصيدها الحالي، ومصاريفها الثابتة
// (قسط شهري 2,000 + قسط سيارة 1,000 = 3,000 ريال إجمالاً).
const RAGHAD_FINANCIAL_CONTEXT = {
  user_income:     20000,
  current_balance: 16197,
  fixed_expenses:  3000,
};

const AGENTS_META = {
  planner:  { initial: 'س', color: '#14213D', tone: 'tone-planner'  },
  risk:     { initial: 'ن', color: '#C1663B', tone: 'tone-risk'     },
  behavior: { initial: 'ف', color: '#8A8171', tone: 'tone-behavior' },
};

// ─── حالة التطبيق ─────────────────────────────────────────────
let chatHistory       = [];
let busy               = false;
let currentAgentBubble = null;
let roundCount         = 0;
let currentDiscussionRound = 1;
let currentTurnResponses   = [];
let messageQueue       = [];

// ─── متغيرات ودوال العمليات التفاعلية البنكية ومخطط سلمان ────────
let cachedSurplusData = null;
let selectedStockName = "";
let selectedLoanAmount = 50000;

function getArabicTime() {
  const now = new Date();
  let hours = now.getHours();
  const minutes = now.getMinutes().toString().padStart(2, '0');
  const ampm = hours >= 12 ? 'م' : 'ص';
  hours = hours % 12;
  hours = hours ? hours : 12;
  const toArabicNumerals = str => str.replace(/\d/g, d => '٠١٢٣٤٥٦٧٨٩'[d]);
  return `اليوم — ${toArabicNumerals(hours.toString())}:${toArabicNumerals(minutes)} ${ampm}`;
}

function addTransactionDynamic(name, amount, type) {
  const container = document.getElementById('transactions-list');
  if (!container) return;

  const div = document.createElement('div');
  div.className = 'transaction-row';

  const isOut = type === 'out';
  const iconClass = isOut ? 'tx-icon-out' : 'tx-icon-in';
  const icon = isOut ? '<i class="ti ti-file-invoice"></i>' : '<i class="ti ti-cash-banknote"></i>';
  const amountClass = isOut ? 'tx-out' : 'tx-in';
  const prefix = isOut ? '-' : '+';

  div.innerHTML = `
    <div class="tx-icon ${iconClass}">${icon}</div>
    <div class="tx-info">
      <div class="tx-name">${name}</div>
      <div class="tx-date">${getArabicTime()}</div>
    </div>
    <div class="tx-amount ${amountClass}">${prefix}${amount.toLocaleString()}</div>
  `;

  container.insertBefore(div, container.firstChild);
}

// ─── مساعدات DOM ───────────────────────────────────────────────
const $         = id => document.getElementById(id);
const messages  = () => $('messages');
const chatArea  = () => $('chat-area');
const userInput = () => $('user-input');
const sendBtn   = () => $('send-btn');

function scrollBottom() {
  const area = chatArea();
  if (area) area.scrollTo({ top: area.scrollHeight, behavior: 'smooth' });
}

// ══════════════════════════════════════════════════════════════════
// التنقل بين الشاشتين (SPA — بدون إعادة تحميل)
// ══════════════════════════════════════════════════════════════════
function openCouncil() {
  $('screen-bank').classList.remove('active');
  $('screen-council').classList.add('active');
  showTab('chat');
  setTimeout(() => userInput()?.focus(), 400);
}

function openBank() {
  $('screen-council').classList.remove('active');
  $('screen-bank').classList.add('active');
}

// ─── استخدام اقتراح ────────────────────────────────────────────
function useSug(btn) {
  const text = btn.textContent.trim();
  userInput().value = text;
  sendMessage();
}

// ─── إدارة حجم textarea ────────────────────────────────────────
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

// ─── مفتاح Enter ───────────────────────────────────────────────
function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

// ─── حالة المستشار في شريط الأعلى ─────────────────────────────
// state: 'idle' | 'evaluating' | 'typing' | 'done'
function setStatus(agentId, state) {
  const chip = $('chip-' + agentId);
  const dot  = $('dot-' + agentId);
  if (!chip || !dot) return;

  chip.classList.remove('active', 'evaluating', 'typing');
  dot.classList.remove('pulse', 'done');

  if (state === 'evaluating') {
    chip.classList.add('evaluating');
  } else if (state === 'typing') {
    chip.classList.add('active', 'typing');
    dot.classList.add('pulse');
  } else if (state === 'done') {
    chip.classList.add('active');
    dot.classList.add('done');
  }
}

function resetAllStatuses() {
  ['planner', 'risk', 'behavior'].forEach(id => setStatus(id, 'idle'));
}

// ─── إضافة رسالة المستخدم ─────────────────────────────────────
function appendUserMsg(text, isPending = false) {
  const div = document.createElement('div');
  div.className = 'msg-user';
  div.innerHTML = `
    <div class="msg-user-bubble" style="${isPending ? 'opacity:0.55;border:1.5px dashed rgba(244,239,227,0.4);' : ''}">
      ${escapeHtml(text)}
      ${isPending ? '<span style="font-size:11px;opacity:0.75;display:block;margin-top:4px;">⏳ في انتظار دور المجلس...</span>' : ''}
    </div>
  `;
  messages().appendChild(div);
  scrollBottom();
  return div;
}

function activatePendingMsg(div) {
  const bubble = div.querySelector('.msg-user-bubble');
  bubble.style.opacity = '';
  bubble.style.border  = '';
  const hint = bubble.querySelector('span');
  if (hint) hint.remove();
}

// ─── مؤشر الطابور ─────────────────────────────────────────────
function updateQueueBadge() {
  let badge = $('queue-badge');
  if (messageQueue.length === 0) {
    if (badge) badge.remove();
    return;
  }
  if (!badge) {
    badge = document.createElement('div');
    badge.id = 'queue-badge';
    $('screen-council').querySelector('.council-shell').appendChild(badge);
  }
  badge.textContent = `📋 ${messageQueue.length} رسالة في الانتظار`;
}

// ─── فاصل جولة المجلس ─────────────────────────────────────────
function appendRoundDivider() {
  roundCount++;
  currentDiscussionRound = 1; // إعادة تصفير جولة النقاش
}

// ─── فاصل الجولات النقاشية الفرعية داخل الاستشارة (واتساب ستايل) ───
function appendDiscussionRoundDivider(round) {
  if (round <= 1) return;
  scrollBottom();
}

// ─── إنشاء فقاعة وكيل ─────────────────────────────────────────
function createAgentBubble(agentId, agentName, role) {
  const meta = AGENTS_META[agentId];
  const wrap = document.createElement('div');
  wrap.className = `msg-agent ${meta.tone}`;
  wrap.id = `msg-${agentId}-${roundCount}-${currentDiscussionRound}`;
  
  // إخفاء العناوين المتكررة لقروب الواتساب وتوضيح التعقيبات
  const roleText = currentDiscussionRound > 1 ? `${role} (تعقيب وتفنيد)` : role;

  wrap.innerHTML = `
    <div class="agent-avatar typing-anim" style="background:${meta.color};color:#fff;">
      ${meta.initial}
    </div>
    <div class="agent-content">
      <div class="agent-meta">
        <span class="agent-name" style="color:${meta.color}">${agentName}</span>
        <span class="agent-role">${roleText}</span>
      </div>
      <div id="tools-${agentId}-${roundCount}-${currentDiscussionRound}"></div>
      <div class="agent-bubble streaming" id="bubble-${agentId}-${roundCount}-${currentDiscussionRound}">
        <div class="typing-dots"><span></span><span></span><span></span></div>
      </div>
    </div>
  `;
  messages().appendChild(wrap);
  scrollBottom();
  return $(`bubble-${agentId}-${roundCount}-${currentDiscussionRound}`);
}

// ─── بطاقة استدعاء أداة (شفافية) ───────────────────────────────
function appendToolCallBadge(agentId, toolName) {
  const container = $(`tools-${agentId}-${roundCount}-${currentDiscussionRound}`);
  if (!container) return;
  const TOOL_LABELS = {
    get_simah_credit_report: '🔍 يفحص تقرير سمة الائتماني...',
    get_available_bank_loans: '🏦 يستعرض عروض التمويل المتاحة...',
    simulate_sama_bank_link: '🏦 يستعلم عن بيانات الحساب من ساما...',
    get_saudi_stock_price: '📈 يستعلم عن أسعار الأسهم السعودية...',
    get_real_estate_index: '🏠 يستعلم عن المؤشر العقاري للمدينة...',
    categorize_expenses: '📊 يصنّف ويحلّل قائمة المصاريف...',
  };
  const badge = document.createElement('div');
  badge.className = 'tool-call-badge';
  badge.textContent = TOOL_LABELS[toolName] || `🔧 يستدعي: ${toolName}`;
  container.appendChild(badge);
  scrollBottom();
}

// ─── بطاقة الاقتباسات والمصادر (RAG) ───────────────────────────────
function appendCitations(agentId, sources) {
  const container = $(`tools-${agentId}-${roundCount}-${currentDiscussionRound}`);
  if (!container) return;
  
  // نمنع التكرار إذا تم بث الحدث مرتين
  if (container.querySelector('.citations-wrap')) return;

  const citationsWrap = document.createElement('div');
  citationsWrap.className = 'citations-wrap';
  sources.forEach(src => {
    const badge = document.createElement('span');
    badge.className = 'citation-badge';
    badge.innerHTML = `<i class="ti ti-book-2"></i> ${escapeHtml(src)}`;
    citationsWrap.appendChild(badge);
  });
  container.appendChild(citationsWrap);
  scrollBottom();
}

// ─── الإرسال الرئيسي ──────────────────────────────────────────
function sendMessage() {
  const input = userInput();
  const text = input.value.trim();
  if (!text) return;

  const welcome = $('welcome');
  if (welcome) {
    welcome.style.opacity = '0';
    welcome.style.transition = 'opacity 0.3s';
    setTimeout(() => welcome.remove(), 300);
  }

  input.value = '';
  input.style.height = 'auto';

  if (busy) {
    const pendingDiv = appendUserMsg(text, true);
    messageQueue.push({ text, pendingDiv });
    updateQueueBadge();
    return;
  }

  processMessage(text, null);
}

// ─── معالجة رسالة واحدة (الدورة الفعلية عبر SSE) ──────────────
async function processMessage(text, pendingDiv) {
  busy = true;
  setSendingUI(true);
  resetAllStatuses();

  if (pendingDiv) {
    activatePendingMsg(pendingDiv);
  } else {
    appendUserMsg(text);
  }

  chatHistory.push({ role: 'user', content: text });
  appendRoundDivider();

  try {
    const res = await fetch('/api/council', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        messages: chatHistory,
        // ── حقن بيانات رغد المالية تلقائياً مع كل طلب ──
        financial_context: RAGHAD_FINANCIAL_CONTEXT,
      }),
    });

    if (!res.ok) throw new Error('Server error: ' + res.status);

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer     = '';
    let agentTexts = {};

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let evt;
        try { evt = JSON.parse(line.slice(6)); } catch { continue; }

        switch (evt.type) {

          case 'evaluating':
            setStatus(evt.agentId, 'evaluating');
            break;

          case 'agent_skipped':
            setStatus(evt.agentId, 'idle');
            break;

          case 'discussion_round_start':
            currentDiscussionRound = evt.round;
            appendDiscussionRoundDivider(evt.round);
            break;

          case 'agent_start':
            setStatus(evt.agentId, 'typing');
            currentDiscussionRound = evt.round || currentDiscussionRound;
            currentAgentBubble = createAgentBubble(evt.agentId, evt.agentName, evt.role);
            agentTexts[evt.agentId] = '';
            break;

          // ── شفافية استدعاء الأدوات (مثل تقرير سمة عند فهد) ──
          case 'tool_call':
            appendToolCallBadge(evt.agentId, evt.tool);
            break;

          // ── عرض مستندات الاقتباسات المصدرية ──
          case 'citations':
            appendCitations(evt.agentId, evt.sources);
            break;

          case 'token':
            if (agentTexts[evt.agentId] === '' && currentAgentBubble) {
              currentAgentBubble.innerHTML = '';
              currentAgentBubble.classList.remove('streaming');
            }
            agentTexts[evt.agentId] = (agentTexts[evt.agentId] || '') + evt.token;
            if (currentAgentBubble) {
              currentAgentBubble.innerHTML = formatText(agentTexts[evt.agentId]);
            }
            scrollBottom();
            break;

          case 'agent_done': {
            const wrap = $(`msg-${evt.agentId}-${roundCount}-${currentDiscussionRound}`);
            if (wrap) wrap.querySelector('.agent-avatar')?.classList.remove('typing-anim');
            if (currentAgentBubble) currentAgentBubble.classList.remove('streaming');
            setStatus(evt.agentId, 'done');
            
            // تجميع رد المستشار لحفظه في تاريخ المحادثة
            const text = agentTexts[evt.agentId] || '';
            if (text.trim()) {
              currentTurnResponses.push(`[${getAgentName(evt.agentId)}]: ${text}`);
            }
            break;
          }

          case 'council_done': {
            const combined = currentTurnResponses.join('\n\n');
            if (combined.trim()) {
              chatHistory.push({ role: 'assistant', content: combined });
            }
            currentTurnResponses = []; // تصفير مصفوفة ردود الاستشارة
            
            // تحقق إذا كان سؤال المستخدم يحتوي على قرض أو ضائقة مالية لعرض كرت التمويل التفاعلي
            if (detectLoanKeywords(text)) {
              setTimeout(() => {
                appendInteractiveLoanCard();
              }, 800);
            }
            break;
          }

          case 'error':
            appendSystemNote(evt.message, 'error');
            break;
        }
      }
    }

  } catch (err) {
    console.error(err);
    appendSystemNote('تعذّر الاتصال بالخادم المحلي. تأكد أن الخادم وOllama يعملان.', 'error');
  }

  busy = false;
  setSendingUI(false);

  if (messageQueue.length > 0) {
    const next = messageQueue.shift();
    updateQueueBadge();
    await new Promise(r => setTimeout(r, 400));
    processMessage(next.text, next.pendingDiv);
  } else {
    updateQueueBadge();
    userInput()?.focus();
  }
}

// ─── تحديث حالة زر الإرسال أثناء الانشغال ─────────────────────
function setSendingUI(isBusy) {
  const btn = sendBtn();
  const input = userInput();
  btn.disabled = isBusy;
  btn.classList.toggle('is-busy', isBusy);
  btn.innerHTML = isBusy
    ? '<i class="ti ti-loader-2 spin"></i>'
    : '<i class="ti ti-send-2"></i>';
  input.placeholder = isBusy
    ? 'المجلس يتحدث الآن... سؤالك التالي سيُضاف للطابور'
    : 'اكتبي سؤالك المالي هنا... (Shift+Enter لسطر جديد)';
}

// ─── رسائل النظام ──────────────────────────────────────────────
function appendSystemNote(msg, kind = 'error') {
  const div = document.createElement('div');
  div.className = `system-note ${kind}`;
  div.textContent = '⚠ ' + msg;
  messages().appendChild(div);
  scrollBottom();
}

// ─── تنسيق النص ────────────────────────────────────────────────
function formatText(text) {
  return escapeHtml(text)
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g,   '<em>$1</em>')
    .replace(/\n/g,          '<br>');
}

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function getAgentName(id) {
  const names = { planner: 'سلمان', risk: 'نورة', behavior: 'فهد' };
  return names[id] || id;
}

// ─── إدارة المظهر الداكن والفاتح (Theme Management) ───
function toggleTheme() {
  document.body.classList.toggle('dark-theme');
  const isDark = document.body.classList.contains('dark-theme');
  localStorage.setItem('theme', isDark ? 'dark' : 'light');
  const icon = $('theme-icon');
  if (icon) {
    icon.className = isDark ? 'ti ti-moon' : 'ti ti-sun';
  }
}

window.addEventListener('DOMContentLoaded', () => {
  const savedTheme = localStorage.getItem('theme');
  if (savedTheme === 'dark') {
    document.body.classList.add('dark-theme');
    const icon = $('theme-icon');
    if (icon) icon.className = 'ti ti-moon';
  }
});

// ─── إدارة التبويبات واللوحات التفاعلية (Dashboard Views) ───
let pieChartInstance = null;
let lineChartInstance = null;

function showTab(tabId) {
  // 1. إيقاف التبويب النشط السابق وتغيير كلاس active في الأزرار
  document.querySelectorAll('.advisor-chip').forEach(el => el.classList.remove('active'));
  
  // إخفاء كافة لوحات التحكم التفاعلية ومنطقة الشات
  document.getElementById('chat-area').style.display = 'none';
  document.querySelector('.input-wrap').style.display = 'none';
  document.getElementById('dashboard-planner').style.display = 'none';
  document.getElementById('dashboard-risk').style.display = 'none';
  document.getElementById('dashboard-behavior').style.display = 'none';
  
  if (tabId === 'chat') {
    document.getElementById('chip-chat').classList.add('active');
    document.getElementById('chat-area').style.display = 'block';
    document.querySelector('.input-wrap').style.display = 'block';
    scrollBottom();
  } else if (tabId === 'planner') {
    document.getElementById('chip-planner').classList.add('active');
    document.getElementById('dashboard-planner').style.display = 'flex';
    // رندر المخططات البيانية لسلمان
    setTimeout(renderPlannerCharts, 100);
  } else if (tabId === 'risk') {
    document.getElementById('chip-risk').classList.add('active');
    document.getElementById('dashboard-risk').style.display = 'flex';
    updateDashboardBalances();
  } else if (tabId === 'behavior') {
    document.getElementById('chip-behavior').classList.add('active');
    document.getElementById('dashboard-behavior').style.display = 'flex';
    calcFinance();
  }
}

// تحديث وعرض المبالغ الحالية لرغد في لوحة التحكم
function updateDashboardBalances() {
  const formattedBalance = RAGHAD_FINANCIAL_CONTEXT.current_balance.toLocaleString() + " ريال";
  const riskBalance = document.getElementById('db-balance-risk');
  if (riskBalance) riskBalance.textContent = formattedBalance;
  
  // تحديث الرصيد في شاشة البنك
  const bankBalance = document.getElementById('balance-number');
  if (bankBalance) bankBalance.textContent = RAGHAD_FINANCIAL_CONTEXT.current_balance.toLocaleString() + ".00";
}

// 📈 واجهة نورة: منطق شراء الأسهم التفاعلي
let selectedStockId = "";
let selectedStockUnitPrice = 0;

function openOrderModal(stockId, name, price) {
  selectedStockId = stockId;
  selectedStockUnitPrice = price;
  selectedStockName = name;
  
  document.getElementById('modal-stock-name').textContent = `${name} (${stockId})`;
  document.getElementById('modal-stock-price').textContent = `${price} ريال`;
  document.getElementById('modal-quantity').value = 10;
  
  calcTotalOrder();
  
  document.getElementById('order-modal').style.display = 'flex';
}

function closeOrderModal() {
  document.getElementById('order-modal').style.display = 'none';
}

function calcTotalOrder() {
  const qty = parseInt(document.getElementById('modal-quantity').value) || 0;
  const total = qty * selectedStockUnitPrice;
  document.getElementById('modal-total-price').textContent = total.toLocaleString() + " ريال";
}

function executeStockPurchase() {
  const qty = parseInt(document.getElementById('modal-quantity').value) || 0;
  const total = qty * selectedStockUnitPrice;
  
  if (qty <= 0) {
    alert("الرجاء إدخال كمية صالحة.");
    return;
  }
  
  if (RAGHAD_FINANCIAL_CONTEXT.current_balance < total) {
    alert("عذراً، الرصيد الاستثماري الحالي غير كافٍ لإتمام عملية الشراء.");
    return;
  }
  
  // خصم المبلغ من رصيد رغد
  RAGHAD_FINANCIAL_CONTEXT.current_balance -= total;
  updateDashboardBalances();
  closeOrderModal();
  
  // إضافة عملية الشراء ديناميكياً
  addTransactionDynamic(`شراء أسهم ${selectedStockName}`, total, 'out');
  
  // عرض شارة نجاح في الشات
  showTab('chat');
  
  const div = document.createElement('div');
  div.className = 'msg-agent';
  div.innerHTML = `
    <div class="msg-agent-wrapper" style="--c: var(--copper);">
      <div class="agent-name-row"><span class="agent-name">نورة</span> <span class="agent-role">تحليل مخاطر</span></div>
      <div class="agent-bubble">
        لقد تم تنفيذ أمر الشراء الفوري لـ <strong>${qty} أسهم</strong> في سهم <strong>${selectedStockId}</strong> بنجاح عبر محفظة الإنماء الاستثمارية بقيمة إجمالية قدرها <strong>${total.toLocaleString()} ريال</strong>. تم تحديث رصيدك الاستثماري الفعلي.
      </div>
    </div>
  `;
  messages().appendChild(div);
  scrollBottom();
}

// 🏦 واجهة فهد: منطق الحاسبة وطلب التمويل التفاعلي
function calcFinance() {
  const amount = parseFloat(document.getElementById('loan-amount-slider').value);
  const months = parseInt(document.getElementById('loan-months-slider').value);
  
  document.getElementById('loan-amount-val').textContent = amount.toLocaleString() + " ريال";
  document.getElementById('loan-months-val').textContent = months + " شهراً";
  
  // هامش الربح هو 3.99% سنوي
  const years = months / 12;
  const rate = 0.0399;
  const totalProfit = amount * rate * years;
  const monthly = (amount + totalProfit) / months;
  
  document.getElementById('calc-monthly-installment').textContent = Math.round(monthly).toLocaleString() + " ريال";
}

function submitDashboardLoan() {
  const amount = parseFloat(document.getElementById('loan-amount-slider').value);
  const monthly = document.getElementById('calc-monthly-installment').textContent;
  
  // العودة إلى الشات لعرض معالج التوقيع برمز التحقق (OTP)
  showTab('chat');
  
  const div = document.createElement('div');
  div.className = 'msg-agent';
  div.innerHTML = `
    <div class="interactive-loan-card">
      <div class="loan-card-title">
        <i class="ti ti-discount-check"></i> طلب تمويل شخصي معلق — مصرف الإنماء
      </div>
      <div class="loan-card-subtitle">
         أنت بصدد توقيع عقد التمويل الشخصي المرن بمبلغ <strong>${amount.toLocaleString()} ريال</strong> وقسط شهري تقريبي قدره <strong>${monthly}</strong> لمدة سداد <strong>${document.getElementById('loan-months-val').textContent}</strong>. يرجى التوثيق والتوقيع بالتحقق:
      </div>
      <div class="otp-container">
        <div style="font-size:13px;font-weight:600;margin-bottom:12px;text-align:center;line-height:1.5;">
           أدخل رمز التحقق (OTP) المكون من 4 أرقام لتوقيع عقد تمويل الإنماء:
        </div>
        <div class="otp-input-row">
          <input type="text" maxlength="1" class="otp-input" onkeyup="focusNextOtp(this, 1)" id="db-otp-1" />
          <input type="text" maxlength="1" class="otp-input" onkeyup="focusNextOtp(this, 2)" id="db-otp-2" />
          <input type="text" maxlength="1" class="otp-input" onkeyup="focusNextOtp(this, 3)" id="db-otp-3" />
          <input type="text" maxlength="1" class="otp-input" onkeyup="focusNextOtp(this, 4)" id="db-otp-4" />
        </div>
        <button class="otp-submit-btn" onclick="submitDashboardLoanSignature(${amount})">توثيق وتوقيع العقد</button>
      </div>
    </div>
  `;
  messages().appendChild(div);
  scrollBottom();
  
  setTimeout(() => {
    document.getElementById('db-otp-1').focus();
  }, 150);
}

function submitDashboardLoanSignature(amount) {
  const otp1 = document.getElementById('db-otp-1').value;
  const otp2 = document.getElementById('db-otp-2').value;
  const otp3 = document.getElementById('db-otp-3').value;
  const otp4 = document.getElementById('db-otp-4').value;
  const otp = otp1 + otp2 + otp3 + otp4;
  
  if (otp.length < 4 || isNaN(otp)) {
    alert("الرجاء إدخال رمز التحقق المكون من 4 أرقام.");
    return;
  }
  
  // إضافة مبلغ القرض لرصيد رغد
  RAGHAD_FINANCIAL_CONTEXT.current_balance += amount;
  updateDashboardBalances();
  
  // إضافة معاملة التمويل ديناميكياً
  addTransactionDynamic("إيداع تمويل شخصي مرن", amount, 'in');
  
  // استبدال الكارت برسالة النجاح
  const parent = document.getElementById('db-otp-1').closest('.interactive-loan-card');
  parent.innerHTML = `
    <div class="loan-success-box">
      <div class="success-icon-badge"><i class="ti ti-circle-check"></i></div>
      <div class="success-title">تم توقيع العقد وإيداع التمويل بنجاح!</div>
      <div style="font-size:12.5px;opacity:0.9;line-height:1.65;margin-top:6px;text-align:center;">
        تمت إضافة <strong>${amount.toLocaleString()} ريال</strong> إلى حسابك الجاري لدى مصرف الإنماء. رقم التوثيق المرجعي: <span style="font-family:monospace;color:#FFD166;">#ALM-${Math.floor(100000 + Math.random() * 900000)}</span>
      </div>
    </div>
  `;
  
  // تحفيز تحميل ملف PDF تجريبي (محاكاة عقد)
  simulateContractPDFDownload(amount);
}

function simulateContractPDFDownload(amount) {
  // محاكاة تنزيل ملف نصي بسيط كعقد
  const content = `عقد تمويل مصرف الإنماء الرقمي\n======================\nالمبلغ: ${amount} ريال\nالشريك: رغد\nالحالة: معتمد وموثق عبر رمز OTP\nرقم العملية: ALM-${Math.floor(100000 + Math.random() * 900000)}`;
  const blob = new Blob([content], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `Alinma_Financing_Contract_${amount}.txt`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// 📊 واجهة سلمان: منطق رسم المخططات البيانية (Chart.js)
function renderPlannerCharts() {
  const isDark = document.body.classList.contains('dark-theme');
  const textColor = isDark ? '#E5E9F0' : '#14213D';
  const gridColor = isDark ? 'rgba(229, 233, 240, 0.1)' : 'rgba(20, 33, 61, 0.08)';

  // 1. Pie Chart - تقسيم الميزانية
  const pieCtx = document.getElementById('budgetPieChart').getContext('2d');
  if (pieChartInstance) pieChartInstance.destroy();
  
  pieChartInstance = new Chart(pieCtx, {
    type: 'doughnut',
    data: {
      labels: ['الالتزامات الثابتة', 'المصاريف الحيوية والفواتير', 'الادخار والفائض المتوقع'],
      datasets: [{
        data: [3000, 5000, 12000],
        backgroundColor: ['#C1663B', '#8A8171', '#14213D'],
        borderColor: isDark ? '#1A2234' : '#F4EFE3',
        borderWidth: 2
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: textColor,
            font: { family: 'IBM Plex Sans Arabic', size: 11 }
          }
        }
      }
    }
  });

  // 2. Combo Bar & Line Chart - الفائض الشهري للفترتين وخط الادخار المتراكم
  const lineCtx = document.getElementById('surplusLineChart').getContext('2d');
  if (lineChartInstance) lineChartInstance.destroy();

  // توليد الفائض المالي عشوائياً للـ 6 أشهر السابقة (الحقيقي) محصوراً بين 1673 و 3446 ريال
  if (!cachedSurplusData) {
    const actualSurplus = [];
    let totalActual = 0;
    for (let i = 0; i < 6; i++) {
      const val = Math.floor(Math.random() * (3446 - 1673 + 1)) + 1673;
      actualSurplus.push(val);
      totalActual += val;
    }
    const averageSurplus = Math.round(totalActual / 6);
    
    // بناء خط الاتجاه المتراكم للـ 12 شهراً
    const cumulative = [];
    let sum = 0;
    for (let i = 0; i < 6; i++) {
      sum += actualSurplus[i];
      cumulative.push(sum);
    }
    for (let i = 0; i < 6; i++) {
      sum += averageSurplus;
      cumulative.push(sum);
    }

    cachedSurplusData = {
      actualSurplus,
      averageSurplus,
      cumulative
    };
  }

  const labels = [
    'يناير (حقيقي)', 'فبراير (حقيقي)', 'مارس (حقيقي)', 'أبريل (حقيقي)', 'مايو (حقيقي)', 'يونيو (حقيقي)',
    'يوليو (متوقع)', 'أغسطس (متوقع)', 'سبتمبر (متوقع)', 'أكتوبر (متوقع)', 'نوفمبر (متوقع)', 'ديسمبر (متوقع)'
  ];

  const actualBarData = [...cachedSurplusData.actualSurplus, null, null, null, null, null, null];
  const expectedBarData = [null, null, null, null, null, null, 
                           cachedSurplusData.averageSurplus, cachedSurplusData.averageSurplus, cachedSurplusData.averageSurplus,
                           cachedSurplusData.averageSurplus, cachedSurplusData.averageSurplus, cachedSurplusData.averageSurplus];

  lineChartInstance = new Chart(lineCtx, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'الفائض الشهري الفعلي (حقيقي)',
          type: 'bar',
          data: actualBarData,
          backgroundColor: 'rgba(193, 102, 59, 0.85)',
          borderColor: '#C1663B',
          borderWidth: 1.5,
          borderRadius: 6,
          yAxisID: 'y'
        },
        {
          label: 'الفائض الشهري المتوقع (متوسط)',
          type: 'bar',
          data: expectedBarData,
          backgroundColor: 'rgba(20, 33, 61, 0.25)',
          borderColor: 'rgba(20, 33, 61, 0.6)',
          borderWidth: 1.5,
          borderDash: [5, 5],
          borderRadius: 6,
          yAxisID: 'y'
        },
        {
          label: 'الاتجاه المتوقع للادخار المتراكم',
          type: 'line',
          data: cachedSurplusData.cumulative,
          borderColor: '#14213D',
          borderWidth: 3,
          pointBackgroundColor: '#C1663B',
          pointBorderColor: '#14213D',
          pointRadius: 4,
          tension: 0.3,
          fill: false,
          yAxisID: 'y1'
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          grid: { color: gridColor },
          ticks: { color: textColor, font: { family: 'IBM Plex Sans Arabic', size: 10 } }
        },
        y: {
          type: 'linear',
          position: 'left',
          grid: { color: gridColor },
          ticks: { color: textColor, font: { family: 'IBM Plex Sans Arabic', size: 10 } },
          title: {
            display: true,
            text: 'الفائض الشهري (ريال)',
            color: textColor,
            font: { family: 'IBM Plex Sans Arabic', size: 11 }
          }
        },
        y1: {
          type: 'linear',
          position: 'right',
          grid: { drawOnChartArea: false },
          ticks: { color: textColor, font: { family: 'IBM Plex Sans Arabic', size: 10 } },
          title: {
            display: true,
            text: 'الادخار التراكمي المتوقع (ريال)',
            color: textColor,
            font: { family: 'IBM Plex Sans Arabic', size: 11 }
          }
        }
      },
      plugins: {
        legend: {
          labels: { color: textColor, font: { family: 'IBM Plex Sans Arabic', size: 11 } }
        }
      }
    }
  });
}

// ─── محاكاة كرت التمويل التفاعلي والتوقيع برمز التحقق (OTP) ───
function detectLoanKeywords(text) {
  const keywords = ["قرض", "تمويل", "ضائقة", "أزمة", "متعثر", "ديون", "مديون", "سلف", "أحتاج مبلغ", "احتاج مبلغ"];
  const lower = text.toLowerCase();
  return keywords.some(kw => lower.includes(kw));
}

function appendInteractiveLoanCard() {
  const div = document.createElement('div');
  div.className = 'msg-agent';
  div.innerHTML = `
    <div class="interactive-loan-card">
      <div class="loan-card-title">
        <i class="ti ti-discount-check"></i> تم قبول تمويلك المالي بنجاح!
      </div>
      <div class="loan-card-subtitle">
        بناءً على تقريرك الائتماني المعتمد من سمة وملاءمتك المالية، قمنا بتجهيز خيارات التمويل التالية لك من مصرف الإنماء. يرجى اختيار أحد العروض لتوقيع العقد وإكمال العملية:
      </div>
      <div class="loan-options-list" id="loan-options-container">
        <div class="loan-option-item" onclick="selectLoanOption(this, 'التمويل الشخصي المتوافق مع الأحكام الشرعية', '1,125 ريال', 50000)">
          <div class="loan-option-info">
            <span class="loan-option-name">التمويل الشخصي المتوافق مع الأحكام الشرعية</span>
            <span class="loan-option-meta">هامش ربح 3.99% • مدة سداد 60 شهراً</span>
          </div>
          <div class="loan-option-price">1125 ريال <span style="font-size:10px;opacity:0.75;">/ شهر</span></div>
        </div>
        <div class="loan-option-item" onclick="selectLoanOption(this, 'تمويل الطوارئ العاجل', '1,743 ريال', 30000)">
          <div class="loan-option-info">
            <span class="loan-option-name">تمويل الطوارئ العاجل</span>
            <span class="loan-option-meta">هامش ربح 4.25% • مدة سداد 36 شهراً</span>
          </div>
          <div class="loan-option-price">1743 ريال <span style="font-size:10px;opacity:0.75;">/ شهر</span></div>
        </div>
        <div class="loan-option-item" onclick="selectLoanOption(this, 'برنامج إعادة هيكلة وتوحيد الديون (سداد المديونية)', '824 ريال', 60000)">
          <div class="loan-option-info">
            <span class="loan-option-name">إعادة هيكلة وتوحيد الديون</span>
            <span class="loan-option-meta">هامش ربح 3.50% • مدة سداد 84 شهراً</span>
          </div>
          <div class="loan-option-price">824 ريال <span style="font-size:10px;opacity:0.75;">/ شهر</span></div>
        </div>
      </div>
      <div id="loan-otp-section" style="display:none;">
        <div class="otp-container">
          <div style="font-size:13px;font-weight:600;margin-bottom:12px;text-align:center;line-height:1.5;">
             أدخل رمز التحقق (OTP) المكون من 4 أرقام لتوقيع عقد تمويل <span id="selected-loan-name" style="color:#FFD166;display:block;margin-top:4px;"></span>:
          </div>
          <div class="otp-input-row">
            <input type="text" maxlength="1" class="otp-input" onkeyup="focusNextOtp(this, 1)" id="otp-1" />
            <input type="text" maxlength="1" class="otp-input" onkeyup="focusNextOtp(this, 2)" id="otp-2" />
            <input type="text" maxlength="1" class="otp-input" onkeyup="focusNextOtp(this, 3)" id="otp-3" />
            <input type="text" maxlength="1" class="otp-input" onkeyup="focusNextOtp(this, 4)" id="otp-4" />
          </div>
          <button class="otp-submit-btn" onclick="submitLoanSignature()">توثيق وتوقيع العقد</button>
        </div>
      </div>
      <div id="loan-success-section" style="display:none;">
        <div class="loan-success-box">
          <div class="success-icon-badge"><i class="ti ti-circle-check"></i></div>
          <div class="success-title">تهانينا! تم توثيق وتوقيع العقد بنجاح</div>
          <div style="font-size:12.5px;opacity:0.9;line-height:1.65;margin-top:6px;">
            تم اعتماد العقد وإيداع كامل مبلغ التمويل في حسابك الجاري لدى مصرف الإنماء. رقم العملية المرجعي: <span style="font-family:monospace;color:#FFD166;">#ALM-${Math.floor(100000 + Math.random() * 900000)}</span>
          </div>
        </div>
      </div>
    </div>
  `;
  messages().appendChild(div);
  scrollBottom();
}

let selectedLoanProduct = "";

function selectLoanOption(element, productName, price, amount) {
  selectedLoanProduct = productName;
  selectedLoanAmount = amount;
  document.getElementById('selected-loan-name').textContent = productName;
  document.getElementById('loan-options-container').style.display = 'none';
  document.getElementById('loan-otp-section').style.display = 'block';
  scrollBottom();
  
  // Focus first OTP field
  setTimeout(() => {
    document.getElementById('otp-1').focus();
  }, 150);
}

function focusNextOtp(input, index) {
  // Allow only numbers
  input.value = input.value.replace(/[^0-9]/g, '');
  if (input.value.length === 1 && index < 4) {
    const isDb = input.id.startsWith('db-');
    const prefix = isDb ? 'db-otp-' : 'otp-';
    const nextEl = document.getElementById(prefix + (index + 1));
    if (nextEl) nextEl.focus();
  }
}

function submitLoanSignature() {
  const otp1 = document.getElementById('otp-1').value;
  const otp2 = document.getElementById('otp-2').value;
  const otp3 = document.getElementById('otp-3').value;
  const otp4 = document.getElementById('otp-4').value;
  
  const otp = otp1 + otp2 + otp3 + otp4;
  
  if (otp.length < 4 || isNaN(otp)) {
    alert("الرجاء إدخال رمز التحقق المكون من 4 أرقام.");
    return;
  }
  
  // إضافة مبلغ القرض لرصيد رغد
  RAGHAD_FINANCIAL_CONTEXT.current_balance += selectedLoanAmount;
  updateDashboardBalances();
  
  // إضافة معاملة القرض ديناميكياً
  addTransactionDynamic(`إيداع تمويل: ${selectedLoanProduct}`, selectedLoanAmount, 'in');

  document.getElementById('loan-otp-section').style.display = 'none';
  document.getElementById('loan-success-section').style.display = 'block';
  scrollBottom();
}
