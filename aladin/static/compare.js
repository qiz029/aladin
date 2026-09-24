(() => {
  const jobs = JSON.parse(document.getElementById('comparisonData').textContent);
  const candidates = jobs.flatMap(job => job.artifacts.filter(a => a.kind === 'image').map(a => ({...a, job})));
  const sides = [...document.querySelectorAll('.compare-side')];
  function load(side) {
    const item = candidates[Number(side.querySelector('.candidate').value)];
    if (!item) { side.textContent = '这个任务还没有图片。'; return; }
    side.querySelector('img').src = item.url;
    side.querySelector('.candidate-info').textContent = `${item.job.id.slice(0,8)} · ${item.name} · seed ${item.seed}`;
    side.querySelector('.candidate-params').textContent = JSON.stringify({prompt:item.prompt, params:item.params || item.job.params}, null, 2);
    const form = side.querySelector('form'); const review = item.review || {};
    form.elements.anatomy.value = review.anatomy || 'unsure';
    form.elements.matches_request.value = review.matches_request || 'unsure';
    form.elements.preferred.checked = review.preferred || false;
    form.elements.notes.value = review.notes || '';
    side.querySelector('.review-status').textContent = review.updated_at ? '已保存人工评审' : '尚未评审';
    side.querySelector('.repair-link').href = '/apps/edit?source=' + encodeURIComponent(`/jobs/${item.job.id}/artifacts/${item.name}`);
  }
  sides.forEach((side,index) => {
    const selector = side.querySelector('.candidate');
    candidates.forEach((item,i) => selector.add(new Option(`${item.job.id.slice(0,8)} / ${item.name}`, i)));
    selector.value = Math.min(index, candidates.length-1);
    selector.addEventListener('change', () => load(side));
    side.querySelector('form').addEventListener('submit', async e => {
      e.preventDefault();
      const form = e.currentTarget;
      const item = candidates[Number(selector.value)];
      const status = side.querySelector('.review-status');
      const button = form.querySelector('button'); button.disabled = true; selector.disabled = true;
      const body = {anatomy:form.elements.anatomy.value, matches_request:form.elements.matches_request.value,
        preferred:form.elements.preferred.checked, notes:form.elements.notes.value};
      try {
        const response = await fetch(`${item.url}/review`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        if (!response.ok) throw Error('保存失败，请重试');
        item.review = await response.json(); status.textContent = '已保存人工评审';
      } catch(error) { status.textContent = error.message; }
      finally { button.disabled = false; selector.disabled = false; }
    });
    load(side);
  });
  document.getElementById('compareZoom').addEventListener('input', e => {
    document.querySelectorAll('.compare-image').forEach(img => {
      img.style.width = e.target.value + '%'; img.style.height = e.target.value + '%';
    });
  });
})();
