/* Progressive enhancement: all forms and links keep their server-side endpoints. */
(() => {
  const activeNav = document.querySelector('nav [aria-current="page"]');
  if (activeNav && matchMedia('(max-width:720px)').matches) {
    activeNav.parentElement.scrollLeft = Math.max(0, activeNav.offsetLeft - 100);
  }
  fetch('/api/v1/billing').then(r => r.json()).then(b => {
    if (!b.available || !Number.isFinite(b.metered_cost)) return;
    const el = document.getElementById('billing');
    const amount = document.createElement('strong');
    amount.textContent = '$' + b.metered_cost.toFixed(2);
    el.append('本月用量', amount);
    el.append(Number.isFinite(b.balance_estimate) ? '预计余额 $' + b.balance_estimate.toFixed(2) : '已抵扣 $' + b.credits_applied.toFixed(2));
    el.title = 'Modal 账单快照 · ' + b.fetched_at + '\n本期计量 $' + b.metered_cost.toFixed(2) + ' · 应付 $' + b.billed_cost.toFixed(2);
    el.hidden = false;
  }).catch(() => {});

  const box = document.getElementById('lightbox');
  const stage = document.getElementById('lightboxStage');
  const closeButton = document.getElementById('lightboxClose');
  let lastFocus;
  function close() {
    box.hidden = true;
    stage.replaceChildren();
    document.body.style.overflow = '';
    document.querySelector('.sidebar').inert = false;
    document.querySelector('.workspace').inert = false;
    if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  }
  function open(trigger) {
    lastFocus = trigger;
    const media = document.createElement(trigger.dataset.kind === 'video' ? 'video' : 'img');
    media.src = trigger.dataset.src;
    if (media.tagName === 'VIDEO') {
      media.controls = media.autoplay = media.loop = media.muted = media.playsInline = true;
    } else media.alt = trigger.dataset.title || '生成的图片';
    stage.replaceChildren(media);
    document.getElementById('lightboxTitle').textContent = trigger.dataset.title || '';
    const jobLink = document.getElementById('lightboxJob');
    jobLink.hidden = !trigger.dataset.job;
    jobLink.href = trigger.dataset.job || '#';
    document.getElementById('lightboxDownload').href = trigger.dataset.download || trigger.dataset.src;
    box.hidden = false;
    document.body.style.overflow = 'hidden';
    document.querySelector('.sidebar').inert = true;
    document.querySelector('.workspace').inert = true;
    closeButton.focus();
  }
  document.addEventListener('click', event => {
    const trigger = event.target.closest('[data-lightbox]');
    if (trigger && trigger.dataset.src) { event.preventDefault(); open(trigger); return; }
    if (!box.hidden && (event.target === box || event.target.closest('[data-lightbox-close]'))) close();
  });
  document.addEventListener('keydown', event => {
    if (box.hidden) return;
    if (event.key === 'Escape') close();
    if (event.key === 'Tab') {
      const items = [...box.querySelectorAll('a,button,video')].filter(el => !el.hidden);
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });

  document.querySelectorAll('[data-example]').forEach(button => button.addEventListener('click', () => {
    const field = document.getElementById(button.dataset.target || 'brief');
    field.value = button.dataset.example;
    field.focus();
  }));

  document.querySelectorAll('input[type=file][data-preview]').forEach(input => {
    let objectUrl;
    const preview = document.getElementById(input.dataset.preview);
    const original = preview.innerHTML;
    input.addEventListener('change', () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      const file = input.files[0];
      input.setCustomValidity('');
      if (!file) { preview.innerHTML = original; return; }
      if (file.size > Number(input.dataset.maxBytes)) {
        input.setCustomValidity('图片太大，请选择不超过 ' + Math.round(Number(input.dataset.maxBytes) / 1048576) + ' MiB 的 PNG 或 JPEG。');
        preview.innerHTML = original;
        input.reportValidity();
        return;
      }
      objectUrl = URL.createObjectURL(file);
      const image = document.createElement('img');
      image.src = objectUrl; image.alt = '选中的输入图';
      preview.replaceChildren(image);
    });
  });
  const modes = document.querySelectorAll('input[name=mode]');
  function updateMode() {
    const isEdit = document.querySelector('input[name=mode]:checked')?.value === 'edit';
    const prompt = document.getElementById('prompt');
    prompt.required = isEdit;
    prompt.placeholder = isEdit ? '描述要修改的地方，例如：把杯子换成蓝色，保留原来的光线和背景。' : '描述新画面的方向，也可以留空，让原图自然变化。';
    const denoise = document.getElementById('denoise');
    denoise.closest('.w-md').hidden = isEdit;
    denoise.disabled = isEdit;
  }
  if (modes.length) { modes.forEach(el => el.addEventListener('change', updateMode)); updateMode(); }

  // 委托在 document 上：局部刷新后新插入的表单（例如任务结束后出现的「删除任务」）同样生效
  document.addEventListener('submit', async event => {
    const form = event.target.closest('form[data-enhanced]');
    if (!form) return;
    event.preventDefault();
    if (form.dataset.submitting) return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    form.dataset.submitting = '1';
    const button = form.querySelector('[type=submit]');
    const original = button.innerHTML;
    button.disabled = true; button.textContent = '正在提交…';
    let errorBox = form.querySelector('.form-error');
    if (!errorBox) {
      errorBox = document.createElement('p'); errorBox.className = 'form-error';
      errorBox.setAttribute('role', 'alert'); form.append(errorBox);
    }
    errorBox.hidden = true;
    try {
      const response = await fetch(form.action, {method:'POST', body:new FormData(form)});
      if (response.ok && response.redirected) { location.assign(response.url); return; }
      let message = '提交失败（' + response.status + '），请稍后再试。';
      const data = await response.json().catch(() => null);
      if (typeof data?.detail === 'string') message = data.detail;
      else if (Array.isArray(data?.detail)) message = data.detail.map(item => item.msg || item).join('\n');
      throw new Error(message);
    } catch (error) {
      errorBox.textContent = error instanceof TypeError ? '连接中断，提交结果尚未确认。请先到任务历史检查，再决定是否重试。' : error.message;
      errorBox.hidden = false;
      button.disabled = false; button.innerHTML = original; delete form.dataset.submitting;
    }
  });
})();

// 局部刷新：取回同一页，只替换变了的节点。
// 以前是整页 reload，页面会闪、滚动位置会跳、打开的大图会被关掉。
// 约定：可刷新区域标 data-live="名字"，行/卡片标 data-key。
// 列表页里有 [data-live-active] 就定期刷新；任务页在收到新产物时调用 aladinRefreshLive()。
(() => {
  function sameText(a, b) {
    const texts = node => [...node.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('\u0000');
    return a.childNodes.length === b.childNodes.length && texts(a) === texts(b);
  }

  // 尽量保留没变的节点：图片不会重新加载，焦点、展开状态也不丢
  function patch(target, fresh) {
    if (target.isEqualNode(fresh)) return;
    const freshKids = [...fresh.children];
    if (freshKids.length && freshKids.every(el => el.dataset.key)) {
      const existing = new Map([...target.children].filter(el => el.dataset.key).map(el => [el.dataset.key, el]));
      target.replaceChildren(...freshKids.map(el => {
        const old = existing.get(el.dataset.key);
        if (!old) return el;
        patch(old, el);
        return old;
      }));
      return;
    }
    const kids = [...target.children];
    if (target.tagName === fresh.tagName && kids.length && kids.length === freshKids.length
        && sameText(target, fresh)) {
      kids.forEach((el, i) => patch(el, freshKids[i]));
      for (const name of fresh.getAttributeNames()) target.setAttribute(name, fresh.getAttribute(name));
      for (const name of target.getAttributeNames()) if (!fresh.hasAttribute(name)) target.removeAttribute(name);
      return;
    }
    target.replaceWith(fresh);
  }

  // 返回新页面里是否还有进行中的任务；网络失败返回 null
  let inflight = null;
  async function refresh() {
    if (inflight) return inflight;       // 连续几张同时到达时合并成一次请求
    inflight = (async () => {
      try {
        const response = await fetch(location.href, {headers: {Accept: 'text/html'}, cache: 'no-store'});
        if (!response.ok) return null;
        const next = new DOMParser().parseFromString(await response.text(), 'text/html');
        const regions = [...document.querySelectorAll('[data-live]')];
        // 结构变了（例如「进行中」筛选下最后一个任务结束，列表变成空状态）：整页取一次就好
        if (regions.some(region => !next.querySelector(`[data-live="${region.dataset.live}"]`))) {
          location.reload();
          return null;
        }
        for (const region of regions) {
          patch(region, document.adoptNode(next.querySelector(`[data-live="${region.dataset.live}"]`)));
        }
        return Boolean(next.querySelector('[data-live-active]'));
      } catch (error) {
        return null;                     // 网络抖动：下一轮再试
      } finally {
        setTimeout(() => { inflight = null; }, 0);
      }
    })();
    return inflight;
  }
  window.aladinRefreshLive = refresh;

  if (!document.querySelector('[data-live-active]')) return;
  const INTERVAL = 4000;
  const MAX_ROUNDS = 900;            // 约一小时；任务卡在非终态时不无限轮询
  let rounds = 0;
  let timer = null;
  let paused = false;
  function schedule(delay) { clearTimeout(timer); timer = setTimeout(tick, delay); }

  async function tick() {
    const lightbox = document.getElementById('lightbox');
    // 后台标签页不轮询；切回来时由 visibilitychange 立刻补一轮
    if (document.hidden) { paused = true; return; }
    if (lightbox && !lightbox.hidden) { schedule(INTERVAL); return; }
    const active = await refresh();
    if (active !== false && ++rounds < MAX_ROUNDS) schedule(INTERVAL);
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && paused) { paused = false; schedule(0); }
  });
  schedule(INTERVAL);
})();

// 尺度提示：同一个尺度在不同模型下补的词不同（Anima/Pony 放开头的分级词，Qwen/视频放结尾的直白词）
(() => {
  for (const field of document.querySelectorAll('.rating-field')) {
    const policy = JSON.parse(field.querySelector('[data-rating-policy]').textContent);
    const hint = field.querySelector('[data-rating-hint]');
    const model = field.hasAttribute('data-follow-model') ? document.getElementById('model') : null;
    const update = () => {
      const family = model ? (policy.model_families[model.value] || 'qwen') : field.dataset.ratingFamily;
      const spec = policy.families[family];
      const rating = field.querySelector('input[name=rating]:checked')?.value || policy.default_rating;
      const tags = spec.tags[rating] || [];
      hint.textContent = tags.length
        ? `${spec.label}：自动在提示词${spec.placement === 'prefix' ? '开头' : '结尾'}补充 ${tags.join(', ')}（已写过的不重复）`
        : `${spec.label}：不补充标签`;
    };
    field.addEventListener('change', update);
    model?.addEventListener('change', update);
    update();
  }
})();

// 隐私模式开关：状态记在本浏览器（localStorage），默认开
(() => {
  const button = document.getElementById('privacyToggle');
  if (!button) return;
  const root = document.documentElement;
  const sync = () => {
    const on = root.dataset.privacy === 'blur';
    button.setAttribute('aria-pressed', String(on));
    button.textContent = on ? '隐私模式：开' : '隐私模式：关';
  };
  button.addEventListener('click', () => {
    const on = root.dataset.privacy !== 'blur';
    if (on) root.dataset.privacy = 'blur'; else delete root.dataset.privacy;
    try { localStorage.setItem('aladin.privacy', on ? 'on' : 'off'); } catch (e) {}
    sync();
  });
  sync();
})();
