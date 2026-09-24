(() => {
  const models = JSON.parse(document.getElementById('image-models').textContent);
  const selector = document.getElementById('model');
  const caseSelector = document.getElementById('peopleCase');
  fetch('/api/v1/benchmarks/people').then(r => { if (!r.ok) throw Error(); return r.json(); }).then(suite => {
    suite.cases.forEach(item => caseSelector.add(new Option(item.label, item.id)));
    caseSelector.addEventListener('change', () => {
      const item = suite.cases.find(item => item.id === caseSelector.value);
      if (!item) return;
      document.getElementById('prompt').value = item.prompt;
      document.getElementById('images').value = suite.images;
      document.getElementById('seed').value = suite.seed;
      document.querySelector(`[name=size][value=${suite.size}]`).checked = true;
    });
  }).catch(() => { caseSelector.options[0].textContent = '测试场景暂不可用，可直接填写提示词'; });
  selector.addEventListener('change', () => {
    const model = models[selector.value];
    for (const name of ['steps', 'cfg', 'negative']) {
      document.getElementById(name).value = model.defaults[name];
    }
    document.getElementById('steps').max = model.steps[1];
    for (const name of ['sampler', 'scheduler']) {
      const select = document.getElementById(name);
      select.replaceChildren(...model[name + 's'].map(value => {
        const option = new Option(value, value);
        option.selected = value === model.defaults[name];
        return option;
      }));
    }
    for (const radio of document.querySelectorAll('input[name="size"]')) {
      radio.checked = radio.value === model.defaults.size;
      radio.closest('label').querySelector('.hint').textContent = model.sizes[radio.value].hint;
    }
    document.getElementById('model-hint').textContent = model.hint;
    document.getElementById('model-label').textContent = model.label;
  });
})();
