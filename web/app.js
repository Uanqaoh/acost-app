'use strict';
const $ = id => document.getElementById(id);
const iconPaths = {
book:'<rect x="6" y="3" width="13" height="18" rx="2"/><path d="M9 3v18M12 7h4m-4 4h4M4 7h2m-2 5h2m-2 5h2"/>',
layers:'<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
chart:'<path d="M5 20V10m7 10V4m7 16V7" stroke-width="4"/>',
settings:'<path d="m10 3-1 3-3 1-3 3 2 3-1 3 3 2 3-1 2 3 3-2 3 1 2-3-2-3 1-3-3-2-3 1-3-3Z"/><circle cx="12" cy="12" r="3"/>',
refresh:'<path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/>',help:'<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4m0 3v.1"/>',
sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',expense:'<circle cx="12" cy="12" r="9"/><path d="M15 8a5 5 0 1 0 1 7M14 8v7M10 8v7"/>',
budget:'<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v11c0 4 16 4 16 0V6M4 11c0 4 16 4 16 0"/>',
refund:'<circle cx="12" cy="13" r="8"/><path d="M9 2h6M9 9l3 3 3-3m-3 3v6m-3-4h6"/>',wallet:'<rect x="3" y="5" width="18" height="15" rx="3"/><path d="M16 10h5v5h-5a2.5 2.5 0 0 1 0-5ZM5 5V3h13"/>',
trend:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m6 16 5-5 3 2 4-6"/>',pie:'<path d="M12 3v9h9A9 9 0 1 1 12 3Zm3 0a9 9 0 0 1 6 6h-6V3Z"/>',
'arrow-left':'<path d="m13 7-5 5 5 5M8 12h10" stroke-width="3"/>','arrow-right':'<path d="m11 7 5 5-5 5M16 12H6" stroke-width="3"/>',
close:'<path d="m6 6 12 12M6 18 18 6"/>',home:'<path d="m3 10 9-7 9 7v10h-7v-7h-4v7H3V10Z"/>',cart:'<path d="M2 4h3l3 12h11l3-9H6"/><circle cx="10" cy="20" r="1"/><circle cx="18" cy="20" r="1"/>',
food:'<path d="M4 11a8 8 0 0 1 16 0M3 12h18M3 16h18M5 20h14M7 7h.1M12 5h.1M16 8h.1"/>',
car:'<path d="m4 10 2-6h12l2 6M3 10h18v8H3V10Zm2 8v3m14-3v3M6 13h2m8 0h2"/>',bag:'<rect x="4" y="7" width="16" height="14" rx="2"/><path d="M8 9V6a4 4 0 0 1 8 0v3"/>',
other:'<circle cx="12" cy="12" r="9"/><path d="M8 12h.1m4 0h.1m4 0h.1" stroke-width="3"/>',income:'<circle cx="12" cy="12" r="9"/><path d="m8 7 4 4 4-4m-4 4v7m-4-6h8m-8 3h8"/>',transfer:'<path d="M3 7h18m-5-4 5 4-5 4M21 17H3m5-4-5 4 5 4"/>'};
function icon(name){return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths[name]||iconPaths.other}</svg>`;}
document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));
const escapeHTML = s => String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const entryTime = value => value == null || value === '' ? '时间未记录' : value;
const money = n => `${n<0?'-':''}C$${(Math.abs(n)/100).toLocaleString('en-CA',{minimumFractionDigits:2,maximumFractionDigits:2})}`;
const compact = n => (Math.abs(n)/100).toLocaleString('en-CA',{maximumFractionDigits:2,useGrouping:false});
const today = new Intl.DateTimeFormat('en-CA',{timeZone:'America/Toronto',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const state={month:today.slice(0,7),loadedMonth:null,day:today,meta:null,summary:null,entries:[],flow:'expense',categoryType:'expense',query:'',type:'',account:'',category:'',loading:false,lastSync:null,failed:false};
let requestNumber=0, controller=null, receiptObjectUrl=null;
function categoryStyle(name,type='expense'){
 if(type==='refund')return {color:'#ffc83b',icon:'refund'};
 if(type==='income')return {color:'#54c681',icon:'income'};
 if(type==='transfer')return {color:'#79abed',icon:'transfer'};
 if(/住房|房租|房屋/.test(name))return {color:'#f26b70',icon:'home'};
 if(/购物/.test(name))return {color:'#fa9d51',icon:'cart'};
 if(/餐饮|Grocery|Eating|Dining/.test(name))return {color:'#ffca46',icon:/Grocery/.test(name)?'cart':'food'};
 if(/交通/.test(name))return {color:'#7dc3ad',icon:'car'};
 if(/娱乐/.test(name))return {color:'#b69bdd',icon:'bag'};
 return {color:'#81ade0',icon:'other'};
}
async function api(path,signal){const response=await fetch(path,{signal,cache:'no-store',headers:{Accept:'application/json'}});let data;try{data=await response.json();}catch{throw new Error('服务返回内容异常');}if(!response.ok)throw new Error(data.message||'暂时无法读取账本');return data;}
async function load(retry=0){
 if(typeof retry!=='number')retry=0;
 const sequence=++requestNumber, month=state.month; if(controller)controller.abort();controller=new AbortController();const signal=controller.signal;state.loading=true;updateSync();
 try{
  const [meta,summary,first]=await Promise.all([api('/api/meta',signal),api(`/api/summary?month=${month}`,signal),api(`/api/entries?month=${month}&limit=200&offset=0`,signal)]);
  const changed=()=>Object.assign(new Error('账本正在更新，本次内容未完成同步'),{code:'snapshot_changed'});
  if(!Number.isSafeInteger(first.revision)||meta.revision!==first.revision||summary.revision!==first.revision||summary.count!==first.total)throw changed();
  let entries=[...first.entries];
  while(entries.length<first.total){const next=await api(`/api/entries?month=${month}&limit=200&offset=${entries.length}`,signal);if(!next.entries.length||next.revision!==first.revision||next.total!==first.total)throw changed();entries.push(...next.entries);}
  if(entries.length!==first.total||new Set(entries.map(e=>e.id)).size!==first.total)throw changed();
  if(sequence!==requestNumber)return;
  const monthChanged=state.loadedMonth!==month;
  state.meta=meta;state.summary=summary;state.entries=entries;state.loadedMonth=month;state.lastSync=new Date();state.failed=false;state.loading=false;
  if((state.day&&!state.day.startsWith(month))||(monthChanged&&!state.day)){state.day=month===today.slice(0,7)?today:(entries[0]?.date||`${month}-01`);}
  $('error-banner').hidden=true;render();
 }catch(error){if(error.name==='AbortError'||sequence!==requestNumber)return;if(error.code==='snapshot_changed'&&retry<1)return load(retry+1);state.failed=true;state.loading=false;$('error-banner').hidden=false;$('error-text').textContent=`连接失败：${error.message}。${state.lastSync?'下方保留上次成功内容，数据可能已过期。':'尚未读取到账本，请检查展示程序的连接设置。'}`;if(!state.lastSync)$('entries-list').innerHTML='<div class="empty-state"><strong>暂时无法读取账本</strong>连接恢复后再试，当前状态不代表空账本。</div>';updateSync();}
}
function updateSync(){ $('sync-dot').className=`dot ${state.failed?'offline':state.lastSync?'online':''}`;$('sync-status').textContent=state.loading?'正在同步…':state.lastSync?`${state.failed?'数据已过期 · ':'已同步 '}${state.lastSync.toLocaleTimeString('zh-CN',{hour12:false,timeZone:'America/Toronto'})}`:'尚未同步'; }
function render(){
 const m=state.meta,s=state.summary; $('book-name').textContent=m.book;$('demo-badge').hidden=!m.demo;$('dataset-note').textContent=m.demo?'独立演示账本 · 全部为虚构记录':'个人云端账本 · 只读展示';
 $('total-expense').textContent=money(s.expense_cents);$('total-income').textContent=money(s.income_cents);$('total-net').textContent=money(s.net_cents);
 const initialized=m.opening_balances_initialized===true;
 $('total-assets').textContent=initialized?money(s.balance_cents):'未初始化';$('total-assets').classList.toggle('unconfigured',!initialized);
 $('asset-foot').innerHTML=initialized?`${m.demo?'模拟资产余额':'当前账面余额'}<br>截至最新同步 · 含全部账单`:'账户期初余额尚未核对<br>不作为实际资产余额';
 $('month-label').textContent=monthLabel(state.loadedMonth);$('month-input').value=state.loadedMonth;
 updateOptions('account-filter',m.accounts.map(x=>x.name),'全部账户',state.account);updateOptions('category-filter',[...new Set(m.categories.flatMap(x=>[x.name.split('-')[0],x.name]))],'全部分类',state.category);
 updateSync();renderFlow();renderMovement();renderCategories();renderCalendar();renderEntries();
}
function updateOptions(id,items,label,value){$(id).innerHTML=`<option value="">${label}</option>`+items.map(x=>`<option value="${escapeHTML(x)}">${escapeHTML(x)}</option>`).join('');$(id).value=value;}
function monthLabel(month){const [y,m]=month.split('-');return `${y}年${Number(m)}月`;}
function daysInMonth(month=state.loadedMonth){const [y,m]=month.split('-').map(Number);return new Date(y,m,0).getDate();}
function dateFor(day){return `${state.loadedMonth}-${String(day).padStart(2,'0')}`;}
function dayData(date){return state.summary.daily.find(d=>d.date===date)||{date,expense_cents:0,income_cents:0,refund_cents:0};}
function flowAmount(d){return state.flow==='expense'?d.expense_cents:state.flow==='income'?d.income_cents:d.income_cents-d.expense_cents;}
function renderFlow(){
 const days=daysInMonth(),data=Array.from({length:days},(_,i)=>dayData(dateFor(i+1))),values=data.map(flowAmount),high=Math.max(0,...values),low=Math.min(0,...values),span=high-low||1,baseline=-low/span*100,total=values.reduce((a,b)=>a+b,0),label={expense:'支出',income:'收入',net:'结余'}[state.flow];
 $('flow-title').textContent=`${label}统计图`;$('flow-average').textContent=`日均 ${money(Math.round(total/days))}`;
 const selected=state.day?.startsWith(state.loadedMonth)?state.day:data.find(d=>flowAmount(d))?.date||dateFor(1);const selectedDay=Number(selected.slice(-2));
 $('flow-selection').textContent=`${Number(state.loadedMonth.slice(-2))}月${selectedDay}日 ${money(flowAmount(dayData(selected)))}`;
 $('flow-chart').style.setProperty('--baseline',baseline+'%');$('flow-chart').classList.toggle('has-negative',low<0);
 $('flow-chart').innerHTML=data.map((d,i)=>`<button class="chart-day${d.date===state.day?' selected':''}" data-chart-day="${d.date}" title="${d.date} ${label} ${money(values[i])}" aria-label="${d.date} ${label} ${money(values[i])}">${values[i]?`<span class="bar ${state.flow==='income'?'income':''} ${values[i]<0?'negative':''}" style="bottom:${values[i]<0?baseline+values[i]/span*100:baseline}%;height:${Math.max(.6,Math.abs(values[i])/span*100)}%"></span>`:''}</button>`).join('');
 ['flow','asset'].forEach(prefix=>{ $(prefix+'-start').textContent=`${Number(state.loadedMonth.slice(-2))}-1`;$(prefix+'-mid').textContent=`${Number(state.loadedMonth.slice(-2))}-16`;$(prefix+'-end').textContent=`${Number(state.loadedMonth.slice(-2))}-${days}`; });
}
function renderMovement(){
 const days=daysInMonth(),lastDay=state.loadedMonth===today.slice(0,7)?Math.max(Number(today.slice(-2)),...state.entries.map(e=>Number(e.date.slice(-2)))):state.loadedMonth>today.slice(0,7)?Math.max(0,...state.entries.map(e=>Number(e.date.slice(-2)))):days;
 const values=[0];for(let i=1;i<=lastDay;i++){const d=dayData(dateFor(i));values.push(values.at(-1)+d.income_cents-d.expense_cents);}
 const low=Math.min(0,...values),high=Math.max(0,...values),span=high-low||100,x=i=>28+i/days*348,y=v=>135-(v-low)/span*115;
 const points=values.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');let svg='';
 for(let i=0;i<4;i++){const value=low+span/3*i,py=y(value);svg+=`<line class="chart-gridline" x1="28" x2="376" y1="${py}" y2="${py}"/><text x="24" y="${py+3}" text-anchor="end" fill="#999" font-size="8">${(value/100).toFixed(0)}</text>`;}
 for(let i=0;i<=4;i++)svg+=`<line class="chart-gridline" x1="${28+i*87}" x2="${28+i*87}" y1="20" y2="135"/>`;
 svg+=`<polygon points="28,${y(0)} ${points} ${x(values.length-1)},${y(0)}" fill="#ffce4d" fill-opacity=".22"/><polyline points="${points}" fill="none" stroke="#ffc434" stroke-width="2" stroke-linejoin="round"/>`;
 $('movement-chart').innerHTML=`<svg viewBox="0 0 390 150" role="img" aria-label="本月累计净变动 ${money(values.at(-1))}">${svg}</svg>`;$('movement-value').textContent=`本月净变动 ${money(values.at(-1))}`;
}
function categoryGroups(){
 const groups=new Map();for(const entry of state.entries){if(state.categoryType==='income'?entry.type!=='income':!['expense','refund'].includes(entry.type))continue;const name=entry.category||'其他',root=name.split('-')[0];if(!groups.has(root))groups.set(root,{name:root,amount:0,count:0,children:new Map()});const g=groups.get(root),amount=entry.type==='refund'?-entry.amount_cents:entry.amount_cents;g.amount+=amount;if(entry.type!=='refund')g.count++;if(!g.children.has(name))g.children.set(name,{name,amount:0,count:0});const c=g.children.get(name);c.amount+=amount;if(entry.type!=='refund')c.count++;}return [...groups.values()].sort((a,b)=>b.amount-a.amount);
}
function renderCategories(){
 const groups=categoryGroups(),denominator=groups.reduce((sum,g)=>sum+Math.max(0,g.amount),0),income=state.categoryType==='income',total=income?state.summary.income_cents:state.summary.expense_cents;let offset=0;
 let content='<circle cx="150" cy="150" r="99" fill="none" stroke="#f1f1f1" stroke-width="40"/>';
 const circ=2*Math.PI*99;for(const g of groups){if(g.amount<=0||!denominator)continue;const ratio=g.amount/denominator,style=categoryStyle(g.name,income?'income':'expense');content+=`<circle cx="150" cy="150" r="99" fill="none" stroke="${style.color}" stroke-width="40" stroke-dasharray="${ratio*circ} ${circ}" stroke-dashoffset="${-offset*circ}" transform="rotate(-90 150 150)"><title>${escapeHTML(g.name)} ${(ratio*100).toFixed(2)}% ${money(g.amount)}</title></circle>`;offset+=ratio;}
 $('donut').innerHTML=content;$('donut-total').textContent=money(total);$('donut-label').textContent=income?'总收入':'净支出';$('category-title').textContent=income?'收入分类详情':'支出分类详情';
 $('category-list').innerHTML=groups.map(g=>{const style=categoryStyle(g.name,income?'income':'expense'),pct=denominator?Math.max(0,g.amount)/denominator*100:0;return `<div class="category-item"><button class="category-main" data-group="${escapeHTML(g.name)}"><span class="category-icon" style="--icon-color:${style.color}">${icon(style.icon)}</span><span class="category-info"><span class="category-top"><span class="category-name">${escapeHTML(g.name)}<small>${pct.toFixed(2)}%</small></span><span class="category-amount">${money(g.amount)}</span></span><span class="category-track"><span style="width:${pct}%;background:${style.color}"></span></span><span class="category-count">${g.count} 笔</span></span></button><div class="category-children">${[...g.children.values()].map(c=>`<button class="category-child" data-filter-category="${escapeHTML(c.name)}"><span class="mini-icon" style="--icon-color:${style.color}">${icon(categoryStyle(c.name).icon)}</span><span>${escapeHTML(c.name.split('-').slice(1).join('-')||c.name)}</span><span>${money(c.amount)} [${c.count}笔]</span></button>`).join('')}</div></div>`;}).join('')||'<div class="empty-state">本月暂无此类记录</div>';
 $('category-note').textContent=income?'收入不含退款和账户间转账。':groups.some(g=>g.amount<0)?'圆环按正净支出占比；负数分类为退款净额，列于下方。':'已按退款到账日期抵扣支出；占比按正净支出计算。';$('clear-category').hidden=!state.category;
}
function renderCalendar(){
 const [year,month]=state.loadedMonth.split('-').map(Number),first=new Date(year,month-1,1).getDay();let html='';for(let i=0;i<42;i++){const dt=new Date(year,month-1,i-first+1),key=`${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`,outside=dt.getMonth()!==month-1,d=dayData(key),has=state.entries.some(e=>e.date===key);html+=`<button class="calendar-day${outside?' outside':''}${key===state.day?' selected':''}${key===today?' today':''}" data-day="${key}" aria-label="${key}${has?` 支出 ${money(d.expense_cents)} 收入 ${money(d.income_cents)}`:''}" aria-pressed="${key===state.day}"><span class="day-number">${dt.getDate()}</span>${has?`<small class="expense">${d.expense_cents<0?'+':'-'}${compact(d.expense_cents)}</small><small class="income">+${compact(d.income_cents)}</small>`:'<span class="no-entry">—</span>'}</button>`;}$('calendar').innerHTML=html;
}
function matchingEntries(){const q=state.query.trim().toLocaleLowerCase();return state.entries.filter(e=>(!state.day||e.date===state.day)&&(!state.type||(state.type==='expense-net'?['expense','refund'].includes(e.type):e.type===state.type))&&(!state.account||e.account===state.account||e.to_account===state.account)&&(!state.category||e.category===state.category||e.category.startsWith(state.category+'-'))&&(!q||`${e.merchant} ${e.note} ${e.category}`.toLocaleLowerCase().includes(q)));}
function renderEntries(){
 const entries=matchingEntries();let expense=0,income=0;for(const e of entries){if(e.type==='expense')expense+=e.amount_cents;if(e.type==='refund')expense-=e.amount_cents;if(e.type==='income')income+=e.amount_cents;}
 const date=state.day?new Date(state.day+'T12:00:00'):null;$('day-title').textContent=date?`${state.day.slice(5).replace('-','/')} ${['星期日','星期一','星期二','星期三','星期四','星期五','星期六'][date.getDay()]}`:`${monthLabel(state.loadedMonth)} · 全月`;
 $('day-total').textContent=income?`收入 ${money(income)}`:`支出 ${money(expense)}`;$('all-days').textContent=state.day?'整月':'当天';
 $('entries-list').innerHTML=entries.map(e=>{const style=categoryStyle(e.category,e.type),label=e.type==='refund'?'退款入账':e.type==='transfer'?'账户转账':e.merchant||e.category||'未命名账单',sign=['income','refund'].includes(e.type)?'+':e.type==='expense'?'-':'';return `<button class="entry-row" data-entry-id="${e.id}"><span class="entry-icon" style="--icon-color:${style.color}">${icon(style.icon)}</span><span class="entry-info"><span class="entry-first"><span class="entry-name">${escapeHTML(label)}${e.refunded_cents?'<small class="refund-tag">已退款</small>':''}</span><span class="entry-amount">${sign}${money(e.amount_cents)}</span></span><span class="entry-second"><span class="entry-note">${state.day?'':escapeHTML(e.date.slice(5))+' · '}${escapeHTML(entryTime(e.time))} · ${escapeHTML(e.category||'转账')} · ${escapeHTML(e.note||e.merchant||'无备注')}</span><span class="entry-account">${escapeHTML(e.type==='transfer'?e.account+' → '+e.to_account:e.account)}</span></span></span></button>`;}).join('')||`<div class="empty-state"><strong>${state.query||state.category||state.type||state.account?'没有匹配的账单':state.day?'这一天没有账单':'这个月还没有账单'}</strong>${state.day?'可选择其他日期，或切换到整月查看。':'成功连接后读取到的账单会显示在这里。'}</div>`;
 $('entry-count').textContent=`显示 ${entries.length} 笔 · 本月共 ${state.entries.length} 笔`;
}
function selectDay(date){if(date.slice(0,7)!==state.loadedMonth){state.month=date.slice(0,7);state.day=date;load();return;}state.month=state.loadedMonth;state.day=date;renderCalendar();renderEntries();renderFlow();}
function changeMonth(offset){const [y,m]=state.month.split('-').map(Number),date=new Date(y,m-1+offset,1);state.month=`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}`;load();}
function openDialog(title,html){if(receiptObjectUrl){URL.revokeObjectURL(receiptObjectUrl);receiptObjectUrl=null;}$('dialog-title').textContent=title;$('dialog-body').innerHTML=html;if(!$('detail-dialog').open)$('detail-dialog').showModal();}
function detailLine(name,value){return `<div class="detail-line"><span>${name}</span><b>${escapeHTML(value)}</b></div>`;}
async function showEntry(id){
 let entry=state.entries.find(e=>e.id===id);if(!entry){try{entry=(await api('/api/entries/'+encodeURIComponent(id))).entry;}catch(error){openDialog('账单详情',`<p class="dialog-error">${escapeHTML(error.message)}</p>`);return;}}
 const label={expense:'支出',income:'收入',refund:'退款',transfer:'转账'}[entry.type];
 openDialog(`${label}详情`,`<div class="detail-amount ${entry.type}">${money(entry.amount_cents)}</div>${detailLine('商家',entry.merchant||'—')}${detailLine('分类',entry.category||'—')}${detailLine('日期时间',entry.date+' '+entryTime(entry.time))}${detailLine('账户',entry.account+(entry.to_account?' → '+entry.to_account:''))}${entry.refunded_cents?detailLine('已退金额',money(entry.refunded_cents)):''}${detailLine('备注',entry.note||'—')}${detailLine('账单编号',entry.id)}${entry.version?detailLine('版本',entry.version):''}${entry.original_id?`<div class="dialog-actions"><button data-original="${entry.original_id}">查看退款原单</button></div>`:''}${entry.receipt_url?'<div id="receipt-container"><p class="dialog-note">正在读取小票…</p></div>':''}<p class="dialog-note">此展示页仅供查询。${state.meta.demo?'这是一笔虚构的演示记录。':''}</p>`);
 if(entry.receipt_url){const target=$('receipt-container');try{if(!/^\/api\/receipts\/[a-f0-9]{32}\.(png|jpg|webp)$/.test(entry.receipt_url))throw new Error('小票地址无效');const response=await fetch(entry.receipt_url,{cache:'no-store'});if(!response.ok)throw new Error('暂时无法读取小票');const blob=await response.blob();if(!target.isConnected)return;receiptObjectUrl=URL.createObjectURL(blob);target.innerHTML=`<img class="detail-receipt" src="${receiptObjectUrl}" alt="账单小票">`;}catch(error){if(target.isConnected)target.innerHTML=`<p class="dialog-error">${escapeHTML(error.message)}</p>`;}}
}
function openStats(){if(!state.summary)return;const s=state.summary;openDialog(`${monthLabel(state.loadedMonth)} · 收支统计`,`<table class="stat-table"><tbody>${[['支出原额',s.gross_expense_cents],['本月到账退款',s.refund_cents],['净支出',s.expense_cents],['收入',s.income_cents],['本月结余',s.net_cents]].map(([k,v])=>`<tr><th>${k}</th><td>${money(v)}</td></tr>`).join('')}</tbody></table><p class="dialog-note">退款按实际到账月份抵扣支出，不计作收入；账户间转账不计入收支。本月共 ${s.count} 笔记录。${state.meta.demo?'全部金额来自隔离的虚构演示账本。':''}</p>`);}
function openAccounts(){if(!state.meta)return;const verified=state.meta.opening_balances_initialized===true;openDialog('账户与资产',`<p class="dialog-note">${verified?'当前账面余额含全部日期的记录。':'账户期初余额尚未核对。以下为已记账交易产生的净变动，不能作为实际可用余额。'}</p>`+state.meta.accounts.map(a=>`<div class="account-view"><span>${escapeHTML(a.name)}</span><strong>${money(verified?a.balance_cents:a.balance_cents-a.opening_balance_cents)}</strong></div>`).join(''));}
function setFilter(category){state.category=category;state.type=state.categoryType==='income'?'income':'expense-net';$('type-filter').value=state.type;state.day=null;$('search-panel').hidden=false;$('category-filter').value=category;$('clear-category').hidden=false;renderCalendar();renderEntries();renderFlow();}
$('calendar').addEventListener('click',e=>{const button=e.target.closest('[data-day]');if(button)selectDay(button.dataset.day);});
$('flow-chart').addEventListener('click',e=>{const button=e.target.closest('[data-chart-day]');if(button)selectDay(button.dataset.chartDay);});
$('entries-list').addEventListener('click',e=>{const button=e.target.closest('[data-entry-id]');if(button)showEntry(button.dataset.entryId);});
$('dialog-body').addEventListener('click',e=>{const button=e.target.closest('[data-original]');if(button)showEntry(button.dataset.original);});
$('category-list').addEventListener('click',e=>{const button=e.target.closest('[data-filter-category],[data-group]');if(button)setFilter(button.dataset.filterCategory||button.dataset.group);});
$('clear-category').onclick=()=>{state.category='';$('category-filter').value='';$('clear-category').hidden=true;renderEntries();};
$('flow-tabs').addEventListener('click',e=>{const b=e.target.closest('[data-flow]');if(!b||!state.summary)return;state.flow=b.dataset.flow;document.querySelectorAll('[data-flow]').forEach(x=>x.classList.toggle('selected',x===b));renderFlow();});
$('category-tabs').addEventListener('click',e=>{const b=e.target.closest('[data-category-type]');if(!b||!state.summary)return;state.categoryType=b.dataset.categoryType;document.querySelectorAll('[data-category-type]').forEach(x=>x.classList.toggle('selected',x===b));renderCategories();});
$('previous-month').onclick=()=>changeMonth(-1);$('next-month').onclick=()=>changeMonth(1);$('month-input').onchange=e=>{if(/^\d{4}-\d{2}$/.test(e.target.value)){state.month=e.target.value;load();}};
for(const id of ['refresh','sidebar-refresh','retry'])$(id).onclick=load;
$('all-days').onclick=$('day-title').onclick=()=>{if(!state.summary)return;state.day=state.day?null:(state.loadedMonth===today.slice(0,7)?today:state.entries[0]?.date||dateFor(1));renderCalendar();renderEntries();renderFlow();};
$('search-toggle').onclick=()=>{if(!state.summary)return;$('search-panel').hidden=!$('search-panel').hidden;if(!$('search-panel').hidden){state.day=null;$('search-input').focus();renderCalendar();renderEntries();renderFlow();}};
$('search-input').oninput=e=>{state.query=e.target.value;state.day=null;renderCalendar();renderEntries();renderFlow();};
for(const [id,key] of [['type-filter','type'],['account-filter','account'],['category-filter','category']])$(id).onchange=e=>{state[key]=e.target.value;state.day=null;renderCalendar();renderEntries();$('clear-category').hidden=!state.category;};
$('reset-filters').onclick=()=>{state.query=state.type=state.account=state.category='';$('search-input').value='';for(const id of ['type-filter','account-filter','category-filter'])$(id).value='';$('clear-category').hidden=true;renderEntries();};
$('statistics-button').onclick=openStats;
document.querySelectorAll('[data-action]').forEach(button=>button.onclick=()=>{const action=button.dataset.action;if(action==='ledger'){window.scrollTo({top:0,behavior:'smooth'});return;}if(action==='accounts')return openAccounts();if(action==='stats')return openStats();if(action==='settings')openDialog('连接设置',`<p class="dialog-note">此页面通过 Mac 展示程序读取你的个人云端账本，访问凭证由展示程序保管。需要更换连接时，打开随程序提供的“配置共享账本”入口。</p>${detailLine('访问权限','只读')}${detailLine('当前账本',state.meta?.demo?'隔离演示账本':'个人正式账本')}${detailLine('自动刷新','页面可见时，每 30 秒一次')}${detailLine('最后同步',state.lastSync?state.lastSync.toLocaleString('zh-CN',{timeZone:'America/Toronto'}):'尚未同步')}`);if(action==='help')openDialog('关于这个账本','<p class="dialog-note">在手机把小票发给 Grok 识别、核对和录入，在这里查看自己的同一份账本。点选日历查看当天账单，点选分类筛选整月记录；点击账单可查看详情和小票。</p><p class="dialog-note">支出为扣除本月到账退款后的净额。收入不含退款及转账。当前没有预算和报销功能，资产期初余额未核实时会明确标注。</p><p class="dialog-note">演示账本中的全部记录均为虚构，与正式账本隔离。</p>');});
$('close-dialog').onclick=()=>$('detail-dialog').close();$('detail-dialog').addEventListener('click',e=>{if(e.target===$('detail-dialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
$('detail-dialog').addEventListener('close',()=>{if(receiptObjectUrl){URL.revokeObjectURL(receiptObjectUrl);receiptObjectUrl=null;}});
setInterval(()=>{if(!document.hidden&&!state.loading)load();},30000);document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!state.loading&&(!state.lastSync||Date.now()-state.lastSync.getTime()>30000))load();});load();
