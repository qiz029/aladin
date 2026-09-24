// LoRA 面板：只在 Pony / Anima 下出现。选择序列化成 [{id, strength}] 写进隐藏字段 loras，
// 与 API 的 loras 字段同形；校验、触发词补充都在服务端（aladin/loras.py），这里只负责选与提示。
(() => {
  const panel = document.getElementById('loraPanel');
  if (!panel) return;
  const catalog = JSON.parse(document.getElementById('loraCatalog').textContent);
  const initial = JSON.parse(document.getElementById('loraSelected').textContent);
  const modelSelect = document.getElementById('model');
  const list = document.getElementById('loraList');
  const input = document.getElementById('lorasInput');
  const warnings = document.getElementById('loraWarnings');
  const summary = document.getElementById('loraSummary');
  const presetSelect = document.getElementById('loraPreset');
  const deletePreset = document.getElementById('loraDeletePreset');
  const prompt = document.getElementById('prompt');
  const byId = Object.fromEntries(catalog.loras.map(item => [item.id, item]));
  const modelsNode = document.getElementById('image-models');
  const models = modelsNode ? JSON.parse(modelsNode.textContent) : {};
  const chipsEnabled = panel.dataset.chips !== 'off' && Boolean(prompt);
  const familyOf = model => Object.entries(catalog.families).find(([, models]) => models.includes(model))?.[0];
  const STORE = 'aladin.loraPresets';
  let family = null;
  let selected = new Map();          // id -> strength
  let settingsNote = '';             // 自动改过采样参数的说明；重绘时保留

  const saved = () => { try { return JSON.parse(localStorage.getItem(STORE) || '[]'); } catch (e) { return []; } };
  const store = presets => { try { localStorage.setItem(STORE, JSON.stringify(presets)); } catch (e) {} };

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'class') node.className = value;
      else if (key.startsWith('data-')) node.setAttribute(key, value);
      else node[key] = value;
    }
    node.append(...children.filter(child => child !== null && child !== undefined));
    return node;
  }

  function sync() {
    input.value = selected.size ? JSON.stringify([...selected].map(([id, strength]) => ({id, strength}))) : '';
    const items = [...selected.keys()].map(id => byId[id]);
    summary.textContent = items.length ? `已选 ${items.length} 个` : '未选';
    const notes = [];
    if (items.length > 4) notes.push(`叠加 ${items.length} 个：每个 LoRA 的强度建议降到 0.5–0.7，否则容易互相打架。`);
    for (const [group, label] of [['pose', '体位 / 视角'], ['style', '画风']]) {
      const same = items.filter(item => item.group === group);
      if (same.length > 1) notes.push(`同时用了 ${same.length} 个${label} LoRA（${same.map(i => i.name).join('、')}），结果常会混在一起或肢体错乱。`);
    }
    const triggers = [...new Set(items.flatMap(item => item.triggers))];
    if (triggers.length) notes.push('生成时自动补充触发词：' + triggers.join(', '));
    warnings.textContent = [settingsNote, ...notes].filter(Boolean).join(' ');
  }

  // 选中带建议采样参数的 LoRA（例如 Anima Turbo）时，把表单的步数/CFG/采样器改成建议值
  function applySettings(item) {
    const changed = [];
    for (const [name, value] of Object.entries(item.settings || {})) {
      const field = document.getElementById(name);
      if (field && String(field.value) !== String(value)) { field.value = value; changed.push(`${name} ${value}`); }
    }
    if (changed.length) settingsNote = `已按 ${item.name} 的建议改为：${changed.join('，')}。`;
  }

  // 取消这类 LoRA 时把采样参数还原成当前模型的默认值，不留在 Turbo 的 CFG 1 / 10 步上
  function restoreSettings(item) {
    const defaults = models[modelSelect.value]?.defaults || {};
    let restored = false;
    for (const name of Object.keys(item.settings || {})) {
      const field = document.getElementById(name);
      if (field && defaults[name] !== undefined) { field.value = defaults[name]; restored = true; }
    }
    if (restored) settingsNote = `已取消 ${item.name}，采样参数恢复为模型默认值。`;
  }

  function row(item) {
    const on = selected.has(item.id);
    const strength = on ? selected.get(item.id) : item.strength;
    const [low, high] = item.range;
    const step = high - low > 3 ? 0.1 : 0.05;
    const check = el('input', {type: 'checkbox', checked: on, 'data-id': item.id});
    const range = el('input', {type: 'range', min: low, max: high, step, value: strength, disabled: !on});
    const number = el('input', {type: 'number', min: low, max: high, step, value: strength, disabled: !on, className: 'lora-number'});
    const setStrength = value => {
      const v = Math.min(high, Math.max(low, Number(value)));
      if (Number.isNaN(v)) return;
      range.value = number.value = v;
      if (selected.has(item.id)) { selected.set(item.id, v); sync(); }
    };
    range.addEventListener('input', () => setStrength(range.value));
    number.addEventListener('change', () => setStrength(number.value));
    check.addEventListener('change', () => {
      if (check.checked) {
        // 真正互斥的组（漫画生成器）选一个就取消另一个；服务端同样会拒绝
        if (item.group === 'comic') for (const other of selected.keys()) if (byId[other].group === 'comic') selected.delete(other);
        selected.set(item.id, Number(number.value));
        applySettings(item);
      } else {
        selected.delete(item.id);
        restoreSettings(item);
      }
      render();
    });
    const chips = chipsEnabled && item.suggest.length ? el('div', {class: 'lora-chips'}, ...item.suggest.map(word =>
      el('button', {type: 'button', className: 'chip-word', textContent: '+ ' + word, title: '加到提示词末尾',
                    onclick: () => {
                      const text = prompt.value.trim();
                      if (!text.toLowerCase().includes(word.toLowerCase())) prompt.value = text ? `${text}, ${word}` : word;
                    }}))) : null;
    const meta = [item.triggers.length ? '触发词：' + item.triggers.join(', ') : '', item.notes].filter(Boolean).join(' · ');
    return el('div', {class: 'lora-row' + (on ? ' on' : '')},
      el('label', {class: 'lora-name'}, check, el('span', {textContent: item.name}),
         el('a', {href: item.url, target: '_blank', rel: 'noopener', className: 'small muted', textContent: '↗', title: 'Civitai 页面'})),
      el('div', {class: 'lora-strength'}, range, number),
      meta ? el('div', {class: 'small muted lora-meta', textContent: meta}) : null,
      on ? chips : null);
  }

  function render() {
    list.replaceChildren();
    const items = catalog.loras.filter(item => item.family === family);
    for (const [kind, label] of catalog.kinds) {
      const group = items.filter(item => item.kind === kind);
      if (!group.length) continue;
      const count = group.filter(item => selected.has(item.id)).length;
      const details = el('details', {class: 'lora-group', open: count > 0 || kind === 'quality'},
        el('summary', {textContent: `${label}（${group.length}）${count ? ` · 已选 ${count}` : ''}`}),
        ...group.map(row));
      list.append(details);
    }
    renderPresets();
    sync();
  }

  function renderPresets() {
    const builtin = catalog.presets.filter(p => p.family === family);
    const mine = saved().filter(p => p.family === family);
    presetSelect.replaceChildren(el('option', {value: '', textContent: '应用预设…'}),
      ...builtin.map(p => el('option', {value: 'builtin:' + p.id, textContent: p.name})),
      ...mine.map(p => el('option', {value: 'mine:' + p.name, textContent: '★ ' + p.name})));
    deletePreset.hidden = true;
  }

  presetSelect.addEventListener('change', () => {
    const [source, key] = presetSelect.value.split(/:(.*)/);
    const preset = source === 'builtin' ? catalog.presets.find(p => p.id === key)
      : saved().find(p => p.family === family && p.name === key);
    if (!preset) return;
    selected = new Map(preset.loras.filter(l => byId[l.id]?.family === family).map(l => [l.id, l.strength]));
    preset.loras.forEach(l => byId[l.id] && applySettings(byId[l.id]));
    render();
    presetSelect.value = source + ':' + key;
    deletePreset.hidden = source !== 'mine';
  });

  document.getElementById('loraSavePreset').addEventListener('click', () => {
    if (!selected.size) { warnings.textContent = '先选几个 LoRA 再存预设。'; return; }
    const name = (window.prompt('预设名称') || '').trim();
    if (!name) return;
    const presets = saved().filter(p => !(p.family === family && p.name === name));
    presets.push({family, name, loras: [...selected].map(([id, strength]) => ({id, strength}))});
    store(presets);
    renderPresets();
    presetSelect.value = 'mine:' + name;
    deletePreset.hidden = false;
  });

  deletePreset.addEventListener('click', () => {
    const [, name] = presetSelect.value.split(/:(.*)/);
    store(saved().filter(p => !(p.family === family && p.name === name)));
    renderPresets();
  });

  document.getElementById('loraClear').addEventListener('click', () => { selected.clear(); settingsNote = ''; render(); });

  function onModel() {
    const next = familyOf(modelSelect.value) || null;
    if (next !== family) {
      family = next;
      settingsNote = '';          // 换了模型，image-models.js 已把采样参数换成新模型的默认值
      // 换底模时清空：LoRA 跨底模会静默无效
      selected = new Map(initial.filter(l => byId[l.id]?.family === family).map(l => [l.id, l.strength]));
      initial.length = 0;
    }
    panel.hidden = !family;
    if (family) render(); else input.value = '';
  }
  modelSelect.addEventListener('change', onModel);
  onModel();
})();
