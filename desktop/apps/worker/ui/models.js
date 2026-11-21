// 识别模型清单：主程序的“本机识别”窗口和 Worker 应用共用（两份相同的文件，tests/test_desktop.py 检查一致）。
// 列出模型的用途、大小、是否已下载；当前识别模式需要的默认勾选，可以改；已下载的可以删除。
(function () {
  const zh = (navigator.language || '').toLowerCase().startsWith('zh')
  const T = zh ? {
    title: '识别模型', mode: '当前识别模式：', unknown: '未知（连不上主程序）', cached: '（上次读到的）',
    whisperjav2: 'WhisperJAV 双遍', whisperjav1: 'WhisperJAV 单遍', faster: 'faster-whisper',
    needed: '当前模式需要', downloaded: '已下载', remove: '删除', confirm: '确认删除',
    download: '下载所选', none: '没有要下载的模型', busy: '正在下载或自检，完成后再操作',
    missing: '当前识别模式需要的模型还没下载：{names}。下载之前，这台电脑不接这个模式的任务。',
  } : {
    title: 'Recognition models', mode: 'Current recognition mode: ', unknown: "unknown (can't reach the main app)", cached: ' (last known)',
    whisperjav2: 'WhisperJAV two-pass', whisperjav1: 'WhisperJAV single-pass', faster: 'faster-whisper',
    needed: 'Needed now', downloaded: 'Downloaded', remove: 'Delete', confirm: 'Confirm delete',
    download: 'Download selected', none: 'Nothing selected', busy: 'Downloading or testing; try again when it finishes',
    missing: "Models the current recognition mode needs aren't downloaded: {names}. This computer won't take jobs in this mode until they are.",
  }

  function size(mb) {
    if (mb == null) return ''
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`
  }

  function modeName(mode) {
    if (!mode) return T.unknown
    const name = mode.mode === 'whisperjav' ? (mode.whisperjav_ensemble ? T.whisperjav2 : T.whisperjav1) : mode.mode === 'faster_whisper' ? T.faster : mode.mode
    return name + (mode.live ? '' : T.cached)
  }

  /**
   * root：放清单的元素。actions.download(ids) / actions.remove(id) 调用外壳命令。
   * 返回 update(catalog, busy)：catalog 是外壳给的模型清单，busy 为 true 时（安装、下载、自检中）按钮不可用。
   */
  window.ModelList = function (root, actions) {
    root.innerHTML = `
      <div class="models-head"><h3>${T.title}</h3><span class="models-mode"></span></div>
      <p class="warn models-missing" hidden></p>
      <div class="models-rows"></div>
      <div class="models-foot"><span class="models-note"></span><button class="primary models-download"></button></div>`
    const rows = root.querySelector('.models-rows')
    const button = root.querySelector('.models-download')
    const note = root.querySelector('.models-note')
    const picked = new Map()  // 用户改过的勾选，刷新后保留
    let confirming = null
    let current = null
    let working = false

    function selection() {
      if (!current) return []
      return current.models.filter((m) => !m.downloaded && (picked.has(m.id) ? picked.get(m.id) : m.needed)).map((m) => m.id)
    }

    function render() {
      const catalog = current
      if (!catalog) return
      root.querySelector('.models-mode').textContent = T.mode + modeName(catalog.mode)
      const missing = catalog.models.filter((m) => catalog.missing.includes(m.id))
      const warn = root.querySelector('.models-missing')
      warn.hidden = !missing.length || !catalog.models.some((m) => m.downloaded)
      warn.textContent = T.missing.replace('{names}', missing.map((m) => `${zh ? m.zh : m.en}（${size(m.size_mb)}）`).join(zh ? '、' : ', '))
      rows.innerHTML = ''
      for (const model of catalog.models) {
        const row = document.createElement('label')
        row.className = 'model-row'
        const checked = picked.has(model.id) ? picked.get(model.id) : model.needed
        row.innerHTML = `
          <input type="checkbox" ${model.downloaded ? 'disabled checked' : checked ? 'checked' : ''} ${working ? 'disabled' : ''}>
          <span class="model-text"><b></b><small></small></span>
          ${model.needed ? `<span class="tag">${T.needed}</span>` : ''}
          <span class="model-size">${size(model.size_mb)}</span>
          ${model.downloaded ? `<span class="model-done">${T.downloaded}</span><button class="model-remove" ${working ? 'disabled' : ''}>${confirming === model.id ? T.confirm : T.remove}</button>` : ''}`
        row.querySelector('b').textContent = zh ? model.zh : model.en
        row.querySelector('small').textContent = zh ? model.purpose_zh : model.purpose_en
        const box = row.querySelector('input')
        box.onchange = () => { picked.set(model.id, box.checked); render() }
        const remove = row.querySelector('.model-remove')
        if (remove) {
          remove.onclick = (event) => {
            event.preventDefault()
            if (confirming !== model.id) {
              confirming = model.id
              render()
              setTimeout(() => { if (confirming === model.id) { confirming = null; render() } }, 4000)
              return
            }
            confirming = null
            actions.remove(model.id)
          }
        }
        rows.appendChild(row)
      }
      const ids = selection()
      const total = catalog.models.filter((m) => ids.includes(m.id)).reduce((sum, m) => sum + (m.size_mb || 0), 0)
      button.textContent = ids.length ? `${T.download}（${size(total)}）` : T.download
      button.disabled = working || !ids.length
      note.textContent = working ? T.busy : ids.length ? '' : T.none
    }

    button.onclick = () => {
      const ids = selection()
      if (ids.length) actions.download(ids)
    }

    return function update(catalog, busy) {
      // 只在内容变化时重画，免得打断正在点的勾选框。
      const key = JSON.stringify([catalog, busy, confirming])
      if (key === root.dataset.key) return
      root.dataset.key = key
      current = catalog
      working = busy
      root.hidden = !catalog
      render()
    }
  }
})()
