import {table} from '../components/common/table.js';import {esc} from '../utils/html.js';import {session} from '../hooks/session.js';import {REFERENCE_STATES,RULE_KINDS} from '../constants/enums.js';

function refBadge(status){return `<span class="badge ref-${esc(status)}">${esc(REFERENCE_STATES[status]||status)}</span>`;}
function ruleKindBadge(kind){return `<span class="badge kind-${esc(kind)}">${esc(RULE_KINDS[kind]||kind)}</span>`;}
function fdText(fd){return `${esc((fd.lhs||[]).join(', ')||'∅')} → ${esc(Array.isArray(fd.rhs)?fd.rhs.join(', '):fd.rhs)}`;}

function originText(origin){
  if(!origin)return '';
  const list=Array.isArray(origin)?origin:[origin];
  return list.map(o=>{
    if(o.type==='own')return '目标自有规则';
    if(o.type==='mapped')return `源规则映射（${(o.source_fd?.lhs||[]).join(',')||'∅'}→${o.source_fd?.rhs||''}）`;
    if(o.type==='transitive')return '经未选属性传递保留';
    return o.type||'引用推导';
  }).join('；');
}

export function referencesPage(items,relations,effective){
  const codeOf=id=>relations.find(r=>r.id===id)?.code||`#${id}`;
  const selectedId=effective?.relation_id;
  const readOnly=session.user.role==='viewer';
  const mappingRows=(items||[]).map(r=>[
    `<strong>#${r.id}</strong><small>${esc(codeOf(r.source_relation_id))} → ${esc(codeOf(r.target_relation_id))}</small>`,
    Object.entries(r.mapping).map(([s,t])=>`<span class="map-pair">${esc(s)} ↦ ${esc(t)}</span>`).join(' '),
    refBadge(r.status)+(r.conflict_reason?`<small class="conflict-reason">${esc(r.conflict_reason)}</small>`:''),
    `<button class="small retry-ref" data-id="${r.id}" ${readOnly||r.status==='effective'?'disabled':''}>重试传播</button>`,
  ]);
  const effRules=(effective?.rules||[]).map(x=>[
    `<strong>${fdText(x.fd)}</strong>`,ruleKindBadge(x.kind),
    `<small class="provenance">${esc(originText(x.origin))}</small>`,
  ]);
  const conflicts=(effective?.conflicts||[]).map(c=>
    `<li>引用 #${c.reference_id}（源 ${esc(codeOf(c.source_relation_id))}）：${esc(c.reason)}</li>`).join('');
  return `<div class="split">
    <section>
      <div class="section-top"><h2>跨草案引用登记簿</h2><span>${items.length} 条 / CROSS-DRAFT REFERENCE</span></div>
      ${table(['引用 / 方向','属性映射（源 ↦ 目标）','检查状态','操作'],mappingRows)}
      <div class="notice">合并属性（多个源属性映射到同一目标属性）与引用成环在建立时即拒绝。源规则更正后沿引用链更新，目标自有规则保持原样；传播失败整链保留旧版，重试不会重复产生规则。</div>
      <div class="section-top" style="margin-top:24px"><h2>有效依赖视图</h2><span>自有 + 已生效派生</span></div>
      <label class="effective-select">查看草案
        <select id="effective-relation">${relations.map(r=>`<option value="${r.id}" ${r.id===selectedId?'selected':''}>${esc(r.code)} · ${esc(r.title)}</option>`).join('')}</select>
      </label>
      ${effective?table(['函数依赖','类型','推导来源'],effRules):'<div class="empty">选择草案后加载有效依赖。</div>'}
      ${conflicts?`<ul class="conflict-list">${conflicts}</ul>`:''}
    </section>
    <section class="form-panel">
      <span class="eyebrow">CROSS-DRAFT MAPPING</span>
      <h2>建立跨草案引用</h2>
      <form id="reference-form">
        <label>源草案（提供规则）<select name="source_relation_id">${relations.map(r=>`<option value="${r.id}">${esc(r.code)} · ${esc(r.title)}</option>`).join('')}</select></label>
        <label>目标草案（接收派生规则）<select name="target_relation_id">${relations.map(r=>`<option value="${r.id}">${esc(r.code)} · ${esc(r.title)}</option>`).join('')}</select></label>
        <label>属性映射（每行一条，格式：源属性=目标属性）<textarea name="mapping_text" rows="5" placeholder="客户编号=客户ID&#10;客户名称=客户名"></textarea></label>
        <button class="primary" ${readOnly?'disabled':''}>建立引用（待检查）+</button>
      </form>
      <p class="help">只登记被选取的属性；经未选属性传递得到的依赖会自动保留。建立后状态为“待检查”，传播成功转“已生效”，与目标自有规则决定项冲突则标“冲突”并说明原因。</p>
    </section>
  </div>`;
}
