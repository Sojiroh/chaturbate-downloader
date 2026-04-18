(() => {
  'use strict';

  const POLL_INTERVAL = 2000;
  const API = {
    start:        (username, format, maxDuration) => `/api/download/start?username=${encodeURIComponent(username)}&output_format=${encodeURIComponent(format)}${maxDuration ? '&max_duration=' + encodeURIComponent(maxDuration) : ''}`,
    stop:         (username) => `/api/download/stop/${encodeURIComponent(username)}`,
    stopAll:      '/api/download/stop-all',
    statusAll:    '/api/download/status',
    statusSingle: (username) => `/api/download/status/${encodeURIComponent(username)}`,
    fileDownload: (username) => `/api/download/file/${encodeURIComponent(username)}`,
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
  const selectFormat   = $('#format');
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

  // --- Toast ---
  function toast(message, type = 'info', duration = 4000) {
    const prefixes = { success: '[OK]', error: '[ERR]', info: '[INFO]' };

    const el = document.createElement('div');
    el.className = `toast toast--${type}`;
    el.innerHTML = `
      <span class="toast__prefix">${prefixes[type] || prefixes.info}</span>
      <span class="toast__message">${escapeHtml(message)}</span>
      <button class="toast__close" aria-label="Close">&times;</button>
    `;

    el.querySelector('.toast__close').addEventListener('click', () => removeToast(el));
    toastContainer.appendChild(el);

    const timer = setTimeout(() => removeToast(el), duration);
    el._timer = timer;
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
    try {
      await apiCall(API.stop(username), { method: 'POST' });
      toast(`Download stopped: @${username}`, 'info');
      fetchStatus();
    } catch (err) {
      toast(`Failed to stop: ${err.message}`, 'error');
    }
  }

  // --- Stop All ---
  async function stopAll() {
    if (!confirm('Stop all active downloads?')) return;
    try {
      btnStopAll.disabled = true;
      await apiCall(API.stopAll, { method: 'POST' });
      toast('All downloads stopped', 'info');
      fetchStatus();
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    } finally {
      btnStopAll.disabled = false;
    }
  }

  // --- Delete File ---
  async function deleteFile(filename) {
    try {
      await apiCall(API.deleteFile(filename), { method: 'DELETE' });
      toast(`File deleted: ${filename}`, 'success');
      fetchFiles();
    } catch (err) {
      toast(`Failed to delete: ${err.message}`, 'error');
    }
  }

  // --- Fetch Status ---
  async function fetchStatus() {
    try {
      const resp = await fetch(API.statusAll);
      if (!resp.ok) throw new Error('Server error');
      const data = await resp.json();

      setServerOnline(true);

      const allDownloads = Array.isArray(data) ? data : [];
      const visible = allDownloads.filter(d => d.active || d.error_message || d.elapsed_seconds < 30);
      const actives = allDownloads.filter(d => d.active);
      renderActiveDownloads(visible.length > 0 ? visible : actives);

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
  function renderActiveDownloads(downloads) {
    const count = downloads.length;
    activeCount.textContent = count;
    btnStopAll.hidden = count === 0;
    activeDot.classList.toggle('is-active', count > 0);

    if (count === 0) {
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
    div.textContent = '[NO ACTIVE DOWNLOADS]';
    return div;
  }

  function isCardActive(d) {
    if (d.error_message) return false;
    if (d.status === 'done' || d.status === 'error') return false;
    return d.is_live === true || d.status === 'converting' || d.status === 'starting';
  }

  function setActiveState(card, d) {
    card.classList.toggle('download-card--active', isCardActive(d));
  }

  function setFieldText(card, field, newText) {
    const el = card.querySelector(`[data-field="${field}"]`);
    if (!el) return;
    if (el.textContent !== newText) {
      el.textContent = newText;
      el.classList.remove('stat__value--flash');
      void el.offsetWidth;
      el.classList.add('stat__value--flash');
    }
  }

  function createDownloadCard(d) {
    const card = document.createElement('div');
    card.className = 'download-card';
    card.dataset.username = d.username;
    setActiveState(card, d);

    const fmt = d.output_path ? d.output_path.split('.').pop().toUpperCase() : 'MP4';

    card.innerHTML = `
      <div class="download-card__header">
        <div class="download-card__user">
          <div class="download-card__name">@${escapeHtml(d.username)}</div>
          <div class="download-card__tags">
            <span class="tag">${escapeHtml(fmt)}</span>
            <span class="download-card__live-badge ${d.is_live ? 'is-live' : 'is-offline'}">
              <span class="mini-dot"></span>
              ${d.is_live ? 'LIVE' : 'OFFLINE'}
            </span>
          </div>
        </div>
        <button class="btn btn--destructive btn--sm btn-stop" data-username="${escapeHtml(d.username)}" aria-label="Stop download for ${escapeHtml(d.username)}">Stop</button>
      </div>
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

    card.querySelector('.btn-stop').addEventListener('click', (e) => {
      const user = e.currentTarget.dataset.username;
      stopDownload(user);
    });

    return card;
  }

  function updateDownloadCard(card, d) {
    setActiveState(card, d);

    setFieldText(card, 'speed', (d.speed_mbps || 0).toFixed(2) + ' MB/s');
    setFieldText(card, 'elapsed', formatTime(d.elapsed_seconds));
    setFieldText(card, 'size', formatBytes(d.bytes_downloaded));
    setFieldText(card, 'segments', `${d.downloaded_segments || 0}/${d.total_segments || 0}`);

    const liveBadge = card.querySelector('.download-card__live-badge');
    if (liveBadge) {
      liveBadge.className = `download-card__live-badge ${d.is_live ? 'is-live' : 'is-offline'}`;
      liveBadge.innerHTML = `<span class="mini-dot"></span> ${d.is_live ? 'LIVE' : 'OFFLINE'}`;
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

      const ext = (file.format || '').toUpperCase();
      const sizeStr = file.size ? formatBytes(file.size) : '—';
      const username = file.username || '';

      row.innerHTML = `
        <div class="file-row__info">
          <div class="file-row__name">${escapeHtml(file.filename || 'File')}</div>
          <div class="file-row__details">
            <span>${sizeStr}</span>
            <span>${ext}</span>
            ${username ? `<span>@${escapeHtml(username)}</span>` : ''}
          </div>
        </div>
        <div class="file-row__actions">
          ${username ? `<a class="btn btn--ghost btn--sm" href="${API.fileDownload(username)}" download>Download</a>` : ''}
          <button class="btn btn--ghost btn--sm btn-delete" data-filename="${escapeHtml(file.filename || '')}" aria-label="Delete ${escapeHtml(file.filename || '')}">Delete</button>
        </div>
      `;

      row.querySelector('.btn-delete').addEventListener('click', (e) => {
        const fname = e.currentTarget.dataset.filename;
        if (confirm(`Delete "${fname}"?`)) {
          deleteFile(fname);
        }
      });

      completedContainer.appendChild(row);
    });
  }

  function createEmptyCompleted() {
    const div = document.createElement('div');
    div.className = 'empty-state';
    div.id = 'completed-empty';
    div.textContent = '[NO FILES]';
    return div;
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
    const format = selectFormat.value || 'mp4';
    const maxDuration = inputDuration.value ? inputDuration.value : null;
    startDownload(username, format, maxDuration);
  });

  btnStopAll.addEventListener('click', () => {
    stopAll();
  });

  // --- Initialize ---
  startPolling();
})();
