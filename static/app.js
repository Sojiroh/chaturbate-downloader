(() => {
  'use strict';

  const POLL_INTERVAL = 2000;
  const API = {
    start:        (username, format, maxDuration) => `/api/download/start?username=${encodeURIComponent(username)}&output_format=${encodeURIComponent(format)}${maxDuration ? '&max_duration=' + encodeURIComponent(maxDuration) : ''}`,
    stop:         (username) => `/api/download/stop/${encodeURIComponent(username)}`,
    stopAll:      '/api/download/stop-all',
    statusAll:    '/api/download/status',
    statusSingle: (username) => `/api/download/status/${encodeURIComponent(username)}`,
    fileDownload: (filename) => `/api/downloads/file/${encodeURIComponent(filename)}`,
    listFiles:    '/api/downloads/list',
    deleteFile:   (filename)  => `/api/downloads/${encodeURIComponent(filename)}`,
  };

  let activeDownloads = {};
  let completedFiles = [];
  let pollTimer = null;

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const form           = $('#download-form');
  const inputUser      = $('#username');
  const inputDuration  = $('#max-duration');
  const btnStart       = $('#btn-start');
  const btnStopAll     = $('#btn-stop-all');
  const activeContainer = $('#active-downloads');
  const completedContainer = $('#completed-downloads');
  const activeEmpty    = $('#active-empty');
  const completedEmpty = $('#completed-empty');
  const activeCount    = $('#active-count');
  const activeDot      = $('#active-dot');
  const completedCount = $('#completed-count');
  const serverStatus   = $('#server-status');
  const statusDot      = $('.pulse-dot');
  const toastContainer = $('#toast-container');
  const OUTPUT_FORMAT = 'mp4';

  // --- Toast ---
  function toast(message, type = 'info', duration = 4000) {
    const prefixes = { success: '[OK]', error: '[ERR]', info: '[INFO]' };

    const el = document.createElement('div');
    el.className = `toast toast--${type}`;
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    el.innerHTML = `
      <span class="toast__prefix">${prefixes[type] || prefixes.info}</span>
      <span class="toast__message">${escapeHtml(message)}</span>
      <button class="toast__close" aria-label="Close">&times;</button>
    `;

    const close = el.querySelector('.toast__close');
    close.addEventListener('click', () => removeToast(el));
    el.addEventListener('mouseenter', () => pauseToast(el));
    el.addEventListener('mouseleave', () => resumeToast(el));
    el.addEventListener('focusin', () => pauseToast(el));
    el.addEventListener('focusout', () => resumeToast(el));
    toastContainer.appendChild(el);

    el._remaining = duration;
    el._startedAt = Date.now();
    el._timer = setTimeout(() => removeToast(el), duration);
  }

  function pauseToast(el) {
    if (el._removed || !el._timer) return;
    clearTimeout(el._timer);
    el._timer = null;
    el._remaining -= Date.now() - el._startedAt;
  }

  function resumeToast(el) {
    if (el._removed || el._timer) return;
    el._startedAt = Date.now();
    el._timer = setTimeout(() => removeToast(el), Math.max(1200, el._remaining));
  }

  function removeToast(el) {
    if (el._removed) return;
    el._removed = true;
    clearTimeout(el._timer);
    el.classList.add('toast--exit');
    el.addEventListener('animationend', () => el.remove());
  }

  // --- Utility ---
  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatBytes(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return (bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
  }

  function formatTime(seconds) {
    if (!seconds || seconds <= 0) return '00:00';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }

  // --- API calls ---
  async function apiCall(url, options = {}) {
    const resp = await fetch(url, { ...options });
    if (!resp.ok) {
      const body = await resp.text();
      let msg;
      try { msg = JSON.parse(body).detail || body; } catch { msg = body; }
      throw new Error(msg);
    }
    return resp;
  }

  // --- Start Download ---
  async function startDownload(username, format, maxDuration) {
    try {
      btnStart.disabled = true;
      btnStart.textContent = '[LOADING...]';
      await apiCall(API.start(username, format, maxDuration), { method: 'POST' });
      toast(`Download started: @${username}`, 'success');
      inputUser.value = '';
      inputDuration.value = '';
      fetchStatus();
    } catch (err) {
      toast(`Failed to start: ${err.message}`, 'error');
    } finally {
      btnStart.disabled = false;
      btnStart.textContent = 'Start';
    }
  }

  // --- Stop Download ---
  async function stopDownload(username) {
    const btn = findButtonByData(activeContainer, '.btn-stop', 'username', username);
    try {
      setButtonLoading(btn, '[STOPPING...]');
      await apiCall(API.stop(username), { method: 'POST' });
      toast(`Download stopped: @${username}`, 'info');
      fetchStatus();
    } catch (err) {
      toast(`Failed to stop: ${err.message}`, 'error');
    } finally {
      clearButtonLoading(btn);
    }
  }

  // --- Stop All ---
  async function stopAll() {
    if (!confirm('Stop all active downloads?')) return;
    try {
      btnStopAll.disabled = true;
      btnStopAll.textContent = '[STOPPING...]';
      await apiCall(API.stopAll, { method: 'POST' });
      toast('All downloads stopped', 'info');
      fetchStatus();
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    } finally {
      btnStopAll.disabled = false;
      btnStopAll.textContent = 'Stop All';
    }
  }

  // --- Delete File ---
  async function deleteFile(filename) {
    const btn = findButtonByData(completedContainer, '.btn-delete', 'filename', filename);
    try {
      setButtonLoading(btn, '[DELETING...]');
      await apiCall(API.deleteFile(filename), { method: 'DELETE' });
      toast(`File deleted: ${filename}`, 'success');
      fetchFiles();
    } catch (err) {
      toast(`Failed to delete: ${err.message}`, 'error');
      clearButtonLoading(btn);
      resetDeleteButton(btn, filename);
    }
  }

  function findButtonByData(root, selector, key, value) {
    return Array.from(root.querySelectorAll(selector)).find(btn => btn.dataset[key] === value) || null;
  }

  function setButtonLoading(btn, label) {
    if (!btn) return;
    btn.dataset.originalText = btn.textContent;
    btn.disabled = true;
    btn.classList.add('is-loading');
    btn.textContent = label;
  }

  function clearButtonLoading(btn) {
    if (!btn) return;
    btn.disabled = false;
    btn.classList.remove('is-loading');
    btn.textContent = btn.dataset.originalText || btn.textContent;
    delete btn.dataset.originalText;
  }

  // --- Fetch Status ---
  async function fetchStatus() {
    try {
      const resp = await fetch(API.statusAll);
      if (!resp.ok) throw new Error('Server error');
      const data = await resp.json();

      setServerOnline(true);

      const allDownloads = Array.isArray(data) ? data : [];
      const visible = allDownloads.filter(d => d.active || d.error_message || d.status === 'error' || d.elapsed_seconds < 30);
      const actives = allDownloads.filter(d => d.active);
      renderActiveDownloads(visible.length > 0 ? visible : actives, actives.length);

    } catch {
      setServerOnline(false);
    }
  }

  // --- Fetch Files ---
  async function fetchFiles() {
    try {
      const resp = await fetch(API.listFiles);
      if (!resp.ok) return;
      const data = await resp.json();
      completedFiles = Array.isArray(data) ? data : [];
      renderCompletedFiles(completedFiles);
    } catch {
      // silent
    }
  }

  // --- Server Status ---
  function setServerOnline(online) {
    serverStatus.textContent = online ? 'Online' : 'Disconnected';
    if (statusDot) {
      statusDot.classList.toggle('online', online);
    }
  }

  // --- Render Active Downloads ---
  function renderActiveDownloads(downloads, activeTotal = downloads.filter(d => d.active).length) {
    const count = activeTotal;
    activeCount.textContent = count;
    btnStopAll.hidden = count === 0;
    activeDot.classList.toggle('is-active', count > 0);

    if (downloads.length === 0) {
      activeContainer.innerHTML = '';
      activeContainer.appendChild(activeEmpty || createEmptyActive());
      activeDownloads = {};
      return;
    }

    // Remove empty state if present
    const emptyEl = activeContainer.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    const newMap = {};
    downloads.forEach(d => { newMap[d.username] = d; });

    const existing = activeContainer.querySelectorAll('.download-card');
    existing.forEach(card => {
      const user = card.dataset.username;
      if (!newMap[user]) {
        card.style.transition = 'opacity 0.25s';
        card.style.opacity = '0';
        setTimeout(() => card.remove(), 250);
      }
    });

    downloads.forEach(d => {
      let card = activeContainer.querySelector(`.download-card[data-username="${CSS.escape(d.username)}"]`);
      if (card) {
        updateDownloadCard(card, d);
      } else {
        card = createDownloadCard(d);
        activeContainer.appendChild(card);
      }
    });

    activeDownloads = newMap;
  }

  function createEmptyActive() {
    const div = document.createElement('div');
    div.className = 'empty-state';
    div.id = 'active-empty';
    div.innerHTML = '<strong>[READY]</strong><span>Start with a username above. Live recordings show status and activity while capture or conversion is running.</span>';
    return div;
  }

  function isCardActive(d) {
    if (d.active === true) return true;
    if (d.error_message) return false;
    if (d.status === 'done' || d.status === 'error') return false;
    return d.is_live === true || d.status === 'converting' || d.status === 'starting';
  }

  function setActiveState(card, d) {
    card.classList.toggle('download-card--active', isCardActive(d));
  }

  function setFieldText(card, field, newText, flash = true) {
    const el = card.querySelector(`[data-field="${field}"]`);
    if (!el) return;
    if (el.textContent !== newText) {
      el.textContent = newText;
      if (flash) {
        el.classList.remove('stat__value--flash');
        void el.offsetWidth;
        el.classList.add('stat__value--flash');
      }
    }
  }

  function getStatusLabel(d) {
    const status = String(d.status || '').toLowerCase();
    if (d.error_message || status === 'error') return 'ERROR';
    if (status === 'done' || status === 'completed' || status === 'complete') return 'DONE';
    if (status === 'converting' || status === 'muxing' || status === 'processing') return 'CONVERTING';
    if (isCardActive(d)) return 'RECORDING';
    return String(d.status || 'RECENT').toUpperCase();
  }

  function getStatusClass(label) {
    return String(label).toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
  }

  function statusStripMarkup(d) {
    const active = isCardActive(d);
    const label = getStatusLabel(d);
    const statusClass = getStatusClass(label);
    return `
      <div class="status-strip ${active ? 'status-strip--active' : 'status-strip--idle'} status-strip--${statusClass}" data-field="status-strip" role="status" aria-live="polite" aria-label="Download status for ${escapeHtml(d.username)}: ${escapeHtml(label)}">
        <span class="status-strip__label" data-field="status-label">${escapeHtml(label)}</span>
        <div class="status-strip__track" aria-hidden="true">
          <div class="status-strip__signal"></div>
        </div>
      </div>
    `;
  }

  function updateStatusStrip(card, d) {
    const wrap = card.querySelector('[data-field="status-strip"]');
    const label = card.querySelector('[data-field="status-label"]');
    if (!wrap || !label) return;
    const active = isCardActive(d);
    const statusLabel = getStatusLabel(d);
    wrap.className = `status-strip ${active ? 'status-strip--active' : 'status-strip--idle'} status-strip--${getStatusClass(statusLabel)}`;
    wrap.setAttribute('aria-label', `Download status for ${d.username}: ${statusLabel}`);
    label.textContent = statusLabel;
  }

  function createDownloadCard(d) {
    const card = document.createElement('div');
    card.className = 'download-card';
    card.dataset.username = d.username;
    setActiveState(card, d);

    const fmt = d.output_path ? d.output_path.split('.').pop().toUpperCase() : 'MP4';
    const live = isCardActive(d) && d.is_live;

    card.innerHTML = `
      <div class="download-card__header">
        <div class="download-card__user">
          <div class="download-card__name">@${escapeHtml(d.username)}</div>
          <div class="download-card__tags">
            <span class="tag">${escapeHtml(fmt)}</span>
            <span class="download-card__live-badge ${live ? 'is-live' : 'is-offline'}">
              <span class="mini-dot"></span>
              ${live ? 'LIVE' : 'OFFLINE'}
            </span>
          </div>
        </div>
        ${d.active ? `<button class="btn btn--destructive btn--sm btn-stop" data-username="${escapeHtml(d.username)}" aria-label="Stop download for ${escapeHtml(d.username)}">Stop</button>` : `<span class="tag tag--muted">${escapeHtml(String(d.status || 'recent').toUpperCase())}</span>`}
      </div>
      ${statusStripMarkup(d)}
      <div class="download-card__stats">
        <div class="stat">
          <span class="stat__label">Speed</span>
          <span class="stat__value" data-field="speed">${(d.speed_mbps || 0).toFixed(2)} MB/s</span>
        </div>
        <div class="stat">
          <span class="stat__label">Elapsed</span>
          <span class="stat__value" data-field="elapsed">${formatTime(d.elapsed_seconds)}</span>
        </div>
        <div class="stat">
          <span class="stat__label">Size</span>
          <span class="stat__value" data-field="size">${formatBytes(d.bytes_downloaded)}</span>
        </div>
        <div class="stat">
          <span class="stat__label">Segments</span>
          <span class="stat__value" data-field="segments">${d.downloaded_segments || 0}/${d.total_segments || 0}</span>
        </div>
        ${d.failed_segments > 0 ? `
        <div class="stat stat--error">
          <span class="stat__label">Failed</span>
          <span class="stat__value" data-field="failed">${d.failed_segments}</span>
        </div>
        ` : ''}
      </div>
      ${d.error_message ? `
      <div class="download-card__error" data-field="error">[ERROR] ${escapeHtml(d.error_message)}</div>
      ` : ''}
    `;

    card.querySelector('.btn-stop')?.addEventListener('click', (e) => {
      const user = e.currentTarget.dataset.username;
      stopDownload(user);
    });

    return card;
  }

  function updateDownloadCard(card, d) {
    setActiveState(card, d);
    updateStatusStrip(card, d);

    setFieldText(card, 'speed', (d.speed_mbps || 0).toFixed(2) + ' MB/s', false);
    setFieldText(card, 'elapsed', formatTime(d.elapsed_seconds), false);
    setFieldText(card, 'size', formatBytes(d.bytes_downloaded), false);
    setFieldText(card, 'segments', `${d.downloaded_segments || 0}/${d.total_segments || 0}`);

    const header = card.querySelector('.download-card__header');
    const action = header?.querySelector('.btn-stop, .tag--muted');
    if (header && ((d.active && !action?.classList.contains('btn-stop')) || (!d.active && !action?.classList.contains('tag--muted')))) {
      action?.remove();
      header.insertAdjacentHTML('beforeend', d.active ? `<button class="btn btn--destructive btn--sm btn-stop" data-username="${escapeHtml(d.username)}" aria-label="Stop download for ${escapeHtml(d.username)}">Stop</button>` : `<span class="tag tag--muted">${escapeHtml(String(d.status || 'recent').toUpperCase())}</span>`);
      header.querySelector('.btn-stop')?.addEventListener('click', (e) => stopDownload(e.currentTarget.dataset.username));
    }

    const liveBadge = card.querySelector('.download-card__live-badge');
    if (liveBadge) {
      const live = isCardActive(d) && d.is_live;
      liveBadge.className = `download-card__live-badge ${live ? 'is-live' : 'is-offline'}`;
      liveBadge.innerHTML = `<span class="mini-dot"></span> ${live ? 'LIVE' : 'OFFLINE'}`;
    }

    let errorEl = card.querySelector('[data-field="error"]');
    if (d.error_message) {
      if (!errorEl) {
        errorEl = document.createElement('div');
        errorEl.className = 'download-card__error';
        errorEl.dataset.field = 'error';
        card.appendChild(errorEl);
      }
      errorEl.textContent = '[ERROR] ' + d.error_message;
    } else if (errorEl) {
      errorEl.remove();
    }
  }

  // --- Render Completed Files ---
  function renderCompletedFiles(files) {
    const count = files.length;
    completedCount.textContent = count;

    if (count === 0) {
      completedContainer.innerHTML = '';
      completedContainer.appendChild(completedEmpty || createEmptyCompleted());
      return;
    }

    completedContainer.innerHTML = '';

    files.forEach(file => {
      const row = document.createElement('div');
      row.className = 'file-row';

      const sizeStr = file.size ? formatBytes(file.size) : '—';
      const username = file.username || '';

      row.innerHTML = `
        <div class="file-row__info">
          <div class="file-row__name">${escapeHtml(file.filename || 'File')}</div>
          <div class="file-row__details">
            <span>${sizeStr}</span>
            <span>MP4</span>
            ${username ? `<span>@${escapeHtml(username)}</span>` : ''}
            <span>Local downloads folder</span>
          </div>
        </div>
        <div class="file-row__actions">
          ${file.filename ? `<a class="btn btn--ghost btn--sm" href="${API.fileDownload(file.filename)}" download>Download</a>` : ''}
          <button class="btn btn--danger-ghost btn--sm btn-delete" data-filename="${escapeHtml(file.filename || '')}" aria-label="Delete ${escapeHtml(file.filename || '')}">Delete</button>
        </div>
      `;

      row.querySelector('.btn-delete').addEventListener('click', (e) => {
        const fname = e.currentTarget.dataset.filename;
        armDeleteButton(e.currentTarget, fname);
      });

      completedContainer.appendChild(row);
    });
  }

  function createEmptyCompleted() {
    const div = document.createElement('div');
    div.className = 'empty-state';
    div.id = 'completed-empty';
    div.innerHTML = '<strong>[LOCAL LIBRARY EMPTY]</strong><span>Finished MP4 files will appear here and are saved locally in this app’s downloads folder.</span>';
    return div;
  }

  function armDeleteButton(btn, filename) {
    if (!filename || btn.disabled) return;
    if (btn.dataset.confirming === 'true') {
      clearTimeout(btn._confirmTimer);
      deleteFile(filename);
      return;
    }
    btn.dataset.confirming = 'true';
    btn.classList.add('is-confirming');
    btn.textContent = 'Confirm delete';
    btn.setAttribute('aria-label', `Confirm delete ${filename}`);
    btn._confirmTimer = setTimeout(() => resetDeleteButton(btn, filename), 3500);
  }

  function resetDeleteButton(btn, filename) {
    if (!btn) return;
    btn.dataset.confirming = 'false';
    btn.classList.remove('is-confirming');
    btn.textContent = 'Delete';
    btn.setAttribute('aria-label', `Delete ${filename || btn.dataset.filename || ''}`);
  }

  // --- Polling ---
  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    fetchStatus();
    fetchFiles();
    pollTimer = setInterval(() => {
      fetchStatus();
      fetchFiles();
    }, POLL_INTERVAL);
  }

  // --- Event Listeners ---
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const username = inputUser.value.trim();
    if (!username) {
      toast('Enter a username', 'error');
      inputUser.focus();
      return;
    }
    if (!/^[a-zA-Z0-9_]{1,50}$/.test(username)) {
      toast('Username can only contain letters, numbers, and underscores', 'error');
      inputUser.focus();
      return;
    }
    const format = OUTPUT_FORMAT;
    const maxDuration = inputDuration.value ? inputDuration.value : null;
    startDownload(username, format, maxDuration);
  });

  btnStopAll.addEventListener('click', () => {
    stopAll();
  });

  // --- Initialize ---
  startPolling();
})();
