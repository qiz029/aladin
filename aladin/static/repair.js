(() => {
  const file = document.getElementById('file');
  const enabled = document.getElementById('repairEnabled');
  const canvas = document.getElementById('repairCanvas');
  const ctx = canvas.getContext('2d');
  const region = document.getElementById('region');
  const status = document.getElementById('repairStatus');
  const fields = ['left', 'top', 'right', 'bottom'].map(k => document.getElementById('region-' + k));
  let picture, rectangle, start, version = 0;
  const reuseNode = document.getElementById('reuse-data');
  const reuse = reuseNode ? JSON.parse(reuseNode.textContent) : null;
  let restoredRegion = null;
  let loadingImage = Promise.resolve();
  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!picture) return;
    ctx.drawImage(picture, 0, 0, canvas.width, canvas.height);
    if (rectangle) {
      const [x0,y0,x1,y1] = rectangle;
      ctx.fillStyle = '#7856df44'; ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 3;
      ctx.fillRect(x0*canvas.width,y0*canvas.height,(x1-x0)*canvas.width,(y1-y0)*canvas.height);
      ctx.strokeRect(x0*canvas.width,y0*canvas.height,(x1-x0)*canvas.width,(y1-y0)*canvas.height);
    }
  }
  function setRegion(value) {
    rectangle = value;
    region.value = value && enabled.checked ? JSON.stringify(value) : '';
    fields.forEach((field,i) => field.value = value ? (value[i]*100).toFixed(1) : '');
    status.textContent = value ? '已选择修复区域；原图会保留在任务记录中。' : '请框选要修复的区域。';
    draw();
  }
  function toggle() {
    document.getElementById('repairControls').hidden = !enabled.checked;
    if (enabled.checked) {
      const edit = document.querySelector('[name=mode][value=edit]');
      edit.checked = true; edit.dispatchEvent(new Event('change'));
    }
    region.value = enabled.checked && rectangle ? JSON.stringify(rectangle) : '';
    if (!enabled.checked) status.textContent = '';
  }
  enabled.addEventListener('change', toggle);
  document.querySelectorAll('[name=mode]').forEach(input => input.addEventListener('change', () => {
    if (input.value !== 'edit') { enabled.checked = false; toggle(); }
  }));
  async function loadPicture() {
    const current = ++version;
    const regionToRestore = restoredRegion;
    restoredRegion = null;
    picture = null; setRegion(null);
    if (!file.files[0]) return;
    const url = URL.createObjectURL(file.files[0]);
    const img = new Image(); img.src = url;
    try {
      await img.decode();
      if (version !== current) return;
      picture = img;
      const scale = Math.min(1, 1000/img.naturalWidth);
      canvas.width = Math.round(img.naturalWidth*scale); canvas.height = Math.round(img.naturalHeight*scale);
      if (regionToRestore) {
        enabled.checked = true; toggle(); setRegion(regionToRestore);
      }
      draw();
    } catch { status.textContent = '无法读取图片，请重新选择 PNG 或 JPEG。'; }
    finally { URL.revokeObjectURL(url); }
  }
  file.addEventListener('change', () => { loadingImage = loadPicture(); });
  function point(e) {
    const r = canvas.getBoundingClientRect();
    return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)), Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))];
  }
  canvas.addEventListener('pointerdown', e => {
    if (!picture) return;
    start = point(e); canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener('pointermove', e => {
    if (!start) return;
    const end = point(e);
    setRegion([Math.min(start[0],end[0]),Math.min(start[1],end[1]),Math.max(start[0],end[0]),Math.max(start[1],end[1])]);
  });
  canvas.addEventListener('pointerup', () => { start = null; });
  canvas.addEventListener('pointercancel', () => { start = null; });
  fields.forEach(field => field.addEventListener('change', () => {
    if (fields.every(f => f.value !== '')) setRegion(fields.map(f => Number(f.value)/100));
    else {
      rectangle = null; region.value = ''; draw();
      status.textContent = '请填写四个边界，或重新拖动框选。';
    }
  }));
  document.getElementById('clearRegion').addEventListener('click', () => setRegion(null));
  file.form.addEventListener('submit', e => {
    if (enabled.checked && (!picture || !rectangle || rectangle[2]<=rectangle[0] || rectangle[3]<=rectangle[1])) {
      e.preventDefault(); e.stopImmediatePropagation();
      status.textContent = '请选择图片并框选一个非空区域。';
    }
  }, true);
  const source = new URLSearchParams(location.search).get('source');
  const input = reuse?.input_url || source;
  if (input && /^\/jobs\/[a-f0-9]{32}\/(input|artifacts\/[\w.-]+\.png)$/.test(input)) {
    if (!reuse) { enabled.checked = true; toggle(); }
    const submit = file.form.querySelector('[type=submit]');
    submit.disabled = true;
    status.textContent = '正在载入原始输入图片…';
    fetch(input).then(r => { if (!r.ok) throw Error(); return r.blob(); }).then(async blob => {
      if (file.files.length) return; // 用户已手动选图时，不覆盖新选择。
      restoredRegion = reuse?.settings.region || null;
      const transfer = new DataTransfer(); transfer.items.add(new File([blob], 'source.png', {type:'image/png'}));
      file.files = transfer.files; file.dispatchEvent(new Event('change'));
      await loadingImage;
      if (!enabled.checked) status.textContent = '已载入原始输入图片。';
    }).catch(() => { status.textContent = '原图加载失败，请手动上传。'; })
      .finally(() => { submit.disabled = false; });
  }
})();
