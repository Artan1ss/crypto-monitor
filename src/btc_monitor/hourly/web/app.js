'use strict';
const $=id=>document.getElementById(id), N=v=>v===null||v===undefined?null:Number(v);
const fmt=(v,d=2)=>N(v)===null?'缺失':N(v).toLocaleString('en-US',{maximumFractionDigits:d,minimumFractionDigits:d});
const date=v=>new Intl.DateTimeFormat('zh-CN',{timeZone:'America/Toronto',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v));
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state={bars:[],signals:[],health:{},selected:null,offline:false};
const colors={text:'#92a5ba',grid:'#243243',up:'#46c6ac',down:'#ee7b88',spot:'#e6ba6c',future:'#7ea8ed'};
const verdict={pending:'追踪中',direction_supported:'方向支持',direction_opposed:'方向相反',insufficient_data:'数据不足',flat:'横盘'};
const delivery={sent:'已发 TG',retrospective:'未发送·历史重建',disabled:'通知关闭',suppressed_cooldown:'冷却期留存',missing_fresh_entry:'缺少实时参考价',invalidated_before_delivery:'通知前已失效',uncertain:'投递结果不确定',attempted:'已尝试·结果待核实',pending:'待发送'};
async function get(path,options){const r=await fetch(path,options);if(!r.ok)throw Error(`请求失败 ${r.status}`);return r.json();}
function filtered(){return state.signals.filter(s=>($('direction').value==='ALL'||s.direction===$('direction').value)&&($('tier').value==='ALL'||s.tier===$('tier').value)&&($('origin').value==='ALL'||s.origin===$('origin').value));}
function summary(){
 const h=state.health; $('status').textContent=state.offline?'离线复盘 · 不发通知':h.enabled?(h.healthy?'1H 监控运行中':'1H 监控预热 / 数据检查'):'1H 监控未启用';
 $('source').textContent=(h.source||state.bars[0]?.source||'未知')==='coinalyze'?'Coinalyze · 3 现货 / 3 合约':'交易所直连 · 4 现货 / 3 合约';
 $('coverage').textContent=`${state.bars.filter(b=>b.complete).length} / ${state.bars.length}`;
 const signals=filtered();$('counts').textContent=`${signals.length} / ${signals.filter(s=>s.delivery_state==='sent').length}`;
 const evaluable=signals.filter(s=>s.origin==='live'&&['direction_supported','direction_opposed','flat'].includes(s.outcomes?.windows?.['24']?.verdict));
 $('performance').textContent=evaluable.length?`${evaluable.filter(s=>s.outcomes.windows['24'].verdict==='direction_supported').length} / ${evaluable.length} 实时样本`:'实时样本尚不足';
 $('notice').textContent=`${state.offline?'离线文件；无法证明云端当前健康。 ':''}1H 收盘后评估；生成箭头≠触发收盘点。${h.source==='direct'?'固定 7 源直连':'Coinalyze 固定 6 源，未包含 OKX 现货'}；费率是未统一周期、非 OI 加权的报告均值。${h.last_evaluation?.blocked?' 当前阻断：'+h.last_evaluation.blocked:''}${h.replay?.state==='running'?' · 历史重建中':''}`;
 $('online').hidden=!state.offline;$('replay').disabled=state.offline||h.replay?.state==='running';
}
function draw(){
 const bars=state.bars;
 if(!bars.length){$('chart').innerHTML='<div class="chart-empty">尚无 1H 特征记录。<br>启用监控后自动生成，或点击“重建历史（不发 TG）”。<br>不会用模拟数据填充真实界面。</div>';return;}
 const W=1100,left=12,right=110,plot=W-left-right,H=790;
 const t0=Date.parse(bars[0].start),t1=Date.parse(bars.at(-1).end)+3600000;
 const x=t=>left+(Date.parse(t)-t0)/(t1-t0)*plot;
 let svg=`<svg viewBox="0 0 ${W} ${H}" aria-label="BTC 1H 价格及市场指标">`;
 const text=(a,b,s,c=colors.text,size=11)=>`<text x="${a}" y="${b}" fill="${c}" font-size="${size}" font-family="system-ui">${esc(s)}</text>`;
 const panels=[['price','价格 · Bybit BTCUSDT / USDT',25,245],['volume','合约成交量 · BTC',283,70],['spot','现货 CVD · BTC · 连续段归零',391,78],['linear','合约 CVD · BTC · 连续段归零',507,78],['oi','同源 OI · BTC · 计数口径未核验',623,55],['funding','报告费率均值 · % · 非 OI 加权',716,45]];
 const cumulative={spot:[],linear:[]};
 for(const market of ['spot','linear']){let sum=0,prior=null;for(const b of bars){if(!b.complete||N(b.aggregated[market].delta_btc)===null){cumulative[market].push(null);sum=0;prior=null;continue;}if(prior!==b.start)sum=0;sum+=N(b.aggregated[market].delta_btc);cumulative[market].push(sum);prior=b.end;}}
 const selected=state.signals.find(s=>s.id===state.selected);
 const series=key=>bars.map((b,i)=>key==='volume'?N(b.aggregated.linear.volume_btc):key==='oi'?N(b.oi_btc):key==='funding'?N(b.funding_fraction_mean)===null?null:N(b.funding_fraction_mean)*100:cumulative[key]?.[i]);
 for(const [key,label,top,height] of panels){
   let values=key==='price'?bars.flatMap(b=>b.price?[N(b.price.low),N(b.price.high)]:[]):series(key).filter(v=>v!==null);
   if(key==='price'&&selected)values.push(N(selected.entry),N(selected.invalidation));
   svg+=text(left,top-8,label);
   if(!values.length){svg+=text(left,top+height/2,'缺少完整数据，未补零');continue;}
   let min=Math.min(...values),max=Math.max(...values);if(['volume','funding'].includes(key)){min=Math.min(min,0);max=Math.max(max,0);}const span=max-min||Math.max(Math.abs(max)*.01,1);min-=span*.06;max+=span*.06;const y=v=>top+height-(v-min)/(max-min)*height;
   for(let j=0;j<3;j++){const val=min+(max-min)*j/2;svg+=`<path d="M${left} ${y(val)}H${W-right+3}" stroke="${colors.grid}"/>`+text(W-right+10,y(val)+4,fmt(val,key==='funding'?4:0));}
   if(key==='price'){
     bars.forEach(b=>{if(!b.price)return;const p=b.price,up=N(p.close)>=N(p.open),c=up?colors.up:colors.down,xx=x(new Date(Date.parse(b.start)+1800000).toISOString()),bw=Math.max(2,plot/bars.length*.55);svg+=`<path d="M${xx} ${y(N(p.high))}V${y(N(p.low))}" stroke="${c}"/><rect x="${xx-bw/2}" y="${Math.min(y(N(p.open)),y(N(p.close)))}" width="${bw}" height="${Math.max(1,Math.abs(y(N(p.open))-y(N(p.close))))}" fill="${c}" opacity="${b.complete?1:.35}"/>`;});
     if(selected){for(const [v,title,c] of [[N(selected.entry),'参考价',colors.future],[N(selected.invalidation),'失效参考',colors.down]])svg+=`<path d="M${left} ${y(v)}H${W-right}" stroke="${c}" stroke-dasharray="5 4"/>`+text(left+5,y(v)-4,`${title} ${fmt(v)}`,c);}
     for(const s of filtered()){const xx=x(s.generated_at),b=bars.find(b=>b.start===s.bar_start);if(!b?.price)continue;const c=s.direction==='SHORT'?colors.down:colors.up,yy=s.direction==='SHORT'?y(N(b.price.high))-16:y(N(b.price.low))+16;svg+=`<g data-signal="${esc(s.id)}" tabindex="0" role="button" aria-label="${esc(s.direction+' '+s.tier+' '+date(s.generated_at))}" cursor="pointer"><title>${esc(s.direction+' '+s.tier+' '+date(s.generated_at)+' · '+s.origin)}</title><path d="${s.direction==='SHORT'?`M${xx-5} ${yy-6}H${xx+5}L${xx} ${yy+3}Z`:`M${xx-5} ${yy+6}H${xx+5}L${xx} ${yy-3}Z`}" stroke="${c}" stroke-width="${s.id===state.selected?3:1.5}" fill="${s.tier==='CONFIRMED'?c:'none'}"/><circle cx="${x(b.end)}" cy="${y(N(b.price.close))}" r="2" fill="${c}"/></g>`;}
   }else{const vals=series(key),c=key==='spot'?colors.spot:colors.future;let path='';vals.forEach((v,i)=>{if(v===null){path+=' ';return;}const xx=x(new Date(Date.parse(bars[i].start)+1800000).toISOString());if(['volume','funding'].includes(key)){const w=Math.max(2,plot/bars.length*.6);svg+=`<rect x="${xx-w/2}" y="${Math.min(y(v),y(0))}" width="${w}" height="${Math.max(1,Math.abs(y(v)-y(0)))}" fill="${v>=0?colors.up:colors.down}" opacity=".7"/>`;}else{const contiguous=i>0&&vals[i-1]!==null&&bars[i-1].end===bars[i].start;path+=`${contiguous?'L':'M'}${xx} ${y(v)} `;}});svg+=`<path d="${path}" stroke="${c}" stroke-width="1.7" fill="none"/>`;}
 }
 for(let i=0;i<bars.length;i+=Math.max(1,Math.ceil(bars.length/8)))svg+=text(x(bars[i].start),785,date(bars[i].start).replace(' ', ' / '),colors.text,10);
 svg+='<path id="crosshair" stroke="#92a5ba" stroke-dasharray="3 4" opacity=".45"/></svg>';$('chart').innerHTML=svg;
 $('chart').querySelectorAll('[data-signal]').forEach(el=>{el.addEventListener('click',()=>select(el.dataset.signal));el.addEventListener('keydown',e=>{if(e.key==='Enter')select(el.dataset.signal);});});
 const node=$('chart').querySelector('svg');node.addEventListener('mousemove',e=>{const rect=node.getBoundingClientRect(),xx=(e.clientX-rect.left)/rect.width*W,t=t0+(xx-left)/plot*(t1-t0),i=Math.floor((t-t0)/3600000),b=bars.find(v=>Date.parse(v.start)===t0+i*3600000);if(!b)return;const p=b.price;$('hover').textContent=`${date(b.start)} ${p?'O '+fmt(p.open)+' H '+fmt(p.high)+' L '+fmt(p.low)+' C '+fmt(p.close):'价格缺失'} | 现货 Δ ${fmt(b.aggregated.spot.delta_btc)} BTC | 合约 Δ ${fmt(b.aggregated.linear.delta_btc)} BTC | ${b.complete?'完整':'缺口'}`;node.querySelector('#crosshair').setAttribute('d',`M${xx} 12V765`);});
}
function ledger(){
 const node=$('signals');node.replaceChildren();for(const s of filtered()){
   const row=document.createElement('button');row.className='signal-row'+(s.id===state.selected?' selected':'');
   const parts=[`${date(s.generated_at)}<small>${s.origin==='live'?'实时记录':'历史重建 · 非实时通知'}</small>`,`<span class="${s.direction==='SHORT'?'short':'long'}">${s.direction} · ${s.tier==='WATCH'?'观察':'确认'}</span><small>${esc(s.kind)}</small>`,esc(s.reasons.join(' / ')),`$${fmt(s.entry)}<small>${esc(delivery[s.delivery_state]||s.delivery_state)}</small>`,`${esc(verdict[s.outcomes?.windows?.['24']?.verdict]||'追踪中')}<small>24H ${fmt(s.outcomes?.windows?.['24']?.signed_return_pct)}%</small>`];
   for(const p of parts){const span=document.createElement('span');span.innerHTML=p;row.append(span);}row.addEventListener('click',()=>select(s.id));node.append(row);
 }if(!filtered().length){const p=document.createElement('p');p.className='signal-empty';p.textContent='该范围没有满足条件的信号。数据不足时不会强行产生提醒。';node.append(p);}
}
async function select(id){
 state.selected=id;let s=state.signals.find(v=>v.id===id);if(!s)return;
 if(!s.snapshot&&!state.offline){try{s=await get('/hourly/signals/'+id);Object.assign(state.signals.find(v=>v.id===id),s);}catch(e){$('status').textContent=e.message;return;}}
 const d=s.snapshot?.diagnostics,c=s.snapshot?.candidate,node=$('detail');node.replaceChildren();
 const title=document.createElement('h2');title.textContent=`${s.direction} · ${s.tier==='WATCH'?'背离观察':'价格确认'}`;node.append(title);
 function row(k,v){const el=document.createElement('div');el.className='detail-row';const a=document.createElement('span'),b=document.createElement('b');a.textContent=k;b.textContent=v;el.append(a,b);node.append(el);}
 row('生成时间',date(s.generated_at));row('触发小时',date(s.bar_start));row('记录来源',s.origin==='live'?'实时':'历史重建（非通知）');row('投递状态',delivery[s.delivery_state]||s.delivery_state);row('参考价格','$'+fmt(s.entry));row('结构失效','$'+fmt(s.invalidation));
 if(d){row('现货 ΔCVD',fmt(d.spot_delta_btc)+' BTC');row('现货 normalizer',fmt(d.spot_normalizer_btc)+' BTC');row('现货主动压力',fmt(N(d.spot_pressure)*100)+'%');row('合约 ΔCVD',fmt(d.futures_delta_btc)+' BTC');row('合约主动压力',fmt(N(d.futures_pressure)*100)+'%');row('价格变化 / ATR',fmt(d.price_change_atr));row('OI 1H',fmt(d.oi_change_pct)+'%');row('报告费率均值',N(d.funding_fraction_mean)===null?'缺失':fmt(N(d.funding_fraction_mean)*100,4)+'%');row('合约量比',fmt(d.volume_ratio)+'x');row('ATR 14H',fmt(d.atr));}
 const ul=document.createElement('ul');for(const reason of s.reasons){const li=document.createElement('li');li.textContent=reason;ul.append(li);}node.append(ul);
 const heading=document.createElement('h3');heading.textContent='后续价格路径 · 非实际交易收益';node.append(heading);
 for(const hours of ['1','4','12','24']){const w=s.outcomes?.windows?.[hours],box=document.createElement('div');box.className='outcome';box.textContent=`${hours}H · ${verdict[w?.verdict]||'追踪中'} · 方向收益 ${fmt(w?.signed_return_pct)}%`;node.append(box);if(w){row('覆盖',`${w.observed_minutes}/${w.expected_minutes} 分钟 · ${w.coverage}`);row('有利/不利极值下界',`${fmt(w.mfe_usdt_lower_bound)} / ${fmt(w.mae_usdt_lower_bound)}`);row('1R / 失效触及次序',w.touch_order);}}
 const disclaimer=document.createElement('p');disclaimer.className='muted';disclaimer.textContent=c?.warnings?.join('；')||'完整分钟之外的路径未知；不把分钟内目标/止损同时触及算作确定获利。';node.append(disclaimer);
 const download=document.createElement('button');download.textContent=state.offline?'下载已导入的信号快照':'下载完整快照 + 原始输入';download.addEventListener('click',async()=>{try{const data=state.offline?s:await get(`/hourly/signals/${id}/export`);const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));a.download=`h1-${id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}catch(e){$('status').textContent=e.message;}});node.append(download);
 if(s.snapshot){const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='完整阈值、诊断与输入血缘';pre.textContent=JSON.stringify(s.snapshot,null,2);details.append(summary,pre);node.append(details);}draw();ledger();
}
async function refresh(){if(state.offline)return;try{const h=$('range').value;const [bars,signals,health]=await Promise.all([get(`/hourly/bars?hours=${h}`),get(`/hourly/signals?hours=${h}&limit=500`),get('/hourly/health')]);Object.assign(state,{bars,signals,health});render();}catch(e){$('status').textContent=e.message;}}
function render(){summary();draw();ledger();if(state.selected&&state.signals.some(s=>s.id===state.selected))select(state.selected);}
for(const id of ['direction','tier','origin'])$(id).addEventListener('change',render);
$('range').addEventListener('change',refresh);$('refresh').addEventListener('click',refresh);
$('replay').addEventListener('click',async()=>{try{const hours=Math.max(30,N($('range').value));await get(`/hourly/replay?hours=${hours}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});$('status').textContent='历史重建已排队，不会发送 TG';await refresh();}catch(e){$('status').textContent=e.message;}});
$('import').addEventListener('change',async e=>{try{const f=e.target.files[0];if(!f)return;if(f.size>50*1024*1024)throw Error('文件超过 50 MiB');const v=JSON.parse(await f.text());if(v.schema!=='btc-hourly-review-v1'||!Array.isArray(v.bars)||!Array.isArray(v.signals)||v.bars.length>168||v.signals.length>500)throw Error('不是有效的 BTC 1H 复盘文件');Object.assign(state,{bars:v.bars,signals:v.signals,health:v.health||{},offline:true,selected:null});render();}catch(error){$('status').textContent=error.message;}});
$('online').addEventListener('click',()=>{state.offline=false;state.selected=null;refresh();});
refresh();setInterval(refresh,30000);
