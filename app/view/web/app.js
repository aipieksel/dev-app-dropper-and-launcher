'use strict';
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));

function preference(key, fallback = '') {
  try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
}
function savePreference(key, value) {
  try { localStorage.setItem(key, value); } catch { notice('This window cannot save browser preferences.', true); }
}

document.documentElement.dataset.theme = preference('appDropperTheme') === 'light' ? 'light' : 'dark';

let noticeTimer = 0;

function notice(message, error = false) {
  const node = $('#notice');
  node.textContent = message;
  node.classList.toggle('error', error);
  node.hidden = !message;
  clearTimeout(noticeTimer);
  if (message) noticeTimer = setTimeout(() => { node.hidden = true; }, error ? 14000 : 7000);
}

async function api(path, data) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-App-Dropper-Token': window.APP_DROPPER_TOKEN,
    },
    body: JSON.stringify(data),
    cache: 'no-store',
  });
  const value = await response.json();
  if (!response.ok) throw Error(value.error || 'The operation failed.');
  return value;
}

async function refresh() {
  const response = await fetch('/api/state', {
    headers: { 'X-App-Dropper-Token': window.APP_DROPPER_TOKEN },
    cache: 'no-store',
  });
  const state = await response.json();
  if (!response.ok) throw Error(state.error || 'Could not load state.');
  render(state.jobs || []);
  $('#statusLine').textContent = state.applications_fallback
    ? 'Apps folder fallback is ' + state.applications_dir
    : 'Apps go to App Dropper Apps and the Apps folder';
}

function render(jobs) {
  const list = $('#jobList');
  list.replaceChildren();
  if (!jobs.length) {
    return;
  }
  for (const job of jobs) {
    const card = document.createElement('article');
    card.className = 'job-card';
    card.innerHTML = '<strong>' + esc(job.name) + '</strong>'
      + '<small>' + esc(job.folder) + '</small>'
      + '<small>' + esc(job.message) + '</small>'
      + '<small>App Dropper Apps: ' + esc(job.dropper_app) + '</small>'
      + '<small>Apps folder: ' + esc(job.applications_app) + '</small>';
    list.append(card);
  }
}

function fileUrlToPath(value) {
  const line = String(value || '').split('\n').map((item) => item.trim()).find((item) => item.startsWith('file:'));
  if (!line) return '';
  try {
    return decodeURIComponent(new URL(line).pathname);
  } catch {
    return '';
  }
}

async function createFromPath(path) {
  const folder = String(path || '').trim();
  if (!folder) {
    notice('Choose a folder path.', true);
    return;
  }
  $('#createApp').disabled = true;
  $('#statusLine').textContent = 'Creating the Mac app…';
  try {
    const job = await api('/api/create', { path: folder });
    notice(job.message || 'Created the Mac app.');
    $('#folderPath').value = '';
    await refresh();
  } catch (error) {
    notice(error.message, true);
    $('#statusLine').textContent = 'Drop a folder to create a Mac app';
  } finally {
    $('#createApp').disabled = false;
  }
}

window.receiveFolderPath = (path) => {
  $('#folderPath').value = path;
  createFromPath(path);
};

$('#themeToggle').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  savePreference('appDropperTheme', next);
};

$('#folderForm').onsubmit = (event) => {
  event.preventDefault();
  createFromPath($('#folderPath').value);
};

$('#chooseFolder').onclick = () => {
  const handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.appDropper;
  if (handler) handler.postMessage('choose-folder');
  else $('#folderPath').focus();
};

$('#openDropper').onclick = () => api('/api/open-folder', { which: 'dropper' }).catch((error) => notice(error.message, true));
$('#openApps').onclick = () => api('/api/open-folder', { which: 'applications' }).catch((error) => notice(error.message, true));

const dropzone = $('#dropzone');
['dragenter', 'dragover'].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add('dragover');
  });
});
['dragleave', 'drop'].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove('dragover');
  });
});
dropzone.addEventListener('drop', (event) => {
  const path = fileUrlToPath(event.dataTransfer.getData('text/uri-list') || event.dataTransfer.getData('text/plain'));
  if (!path) {
    notice('Paste the folder path. This window did not receive the folder location.', true);
    return;
  }
  window.receiveFolderPath(path);
});

refresh().catch((error) => notice(error.message, true));
