// 从任务页 / 收藏 / 对比页的「生成视频」进来时带着 ?source=<图片地址>：取回那张图填进起始图。
// 只认本站的任务产物与收藏文件，其他地址一律忽略（不替用户去拉任意 URL）。
(() => {
  const source = new URLSearchParams(location.search).get('source');
  const file = document.getElementById('file');
  const status = document.getElementById('sourceStatus');
  if (!source || !file) return;
  if (!/^\/jobs\/[a-f0-9]{32}\/artifacts\/[\w.-]+\.(png|jpe?g)$/.test(source) && !/^\/gallery\/\d+\/file$/.test(source)) return;
  const submit = file.form.querySelector('[type=submit]');
  submit.disabled = true;
  status.textContent = '正在载入起始图…';
  fetch(source).then(r => { if (!r.ok) throw Error(); return r.blob(); }).then(blob => {
    if (file.files.length) return;      // 用户已经手动选了图，不覆盖
    const jpeg = blob.type === 'image/jpeg';
    const transfer = new DataTransfer();
    transfer.items.add(new File([blob], jpeg ? 'source.jpg' : 'source.png', {type: jpeg ? 'image/jpeg' : 'image/png'}));
    file.files = transfer.files;
    file.dispatchEvent(new Event('change'));   // 触发预览与大小检查
    status.textContent = '已载入起始图，写好提示词就可以生成。';
  }).catch(() => { status.textContent = '起始图载入失败，请手动上传。'; })
    .finally(() => { submit.disabled = false; });
})();
