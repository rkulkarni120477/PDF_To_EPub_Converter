const fileInput = document.querySelector('#fileInput');
const browseButton = document.querySelector('#browseButton');
const dropzone = document.querySelector('#dropzone');
const dropTitle = document.querySelector('#dropTitle');
const dropText = document.querySelector('#dropText');
const sourceFile = document.querySelector('#sourceFile');
const convertButton = document.querySelector('#convertButton');
const downloadLink = document.querySelector('#downloadLink');
const previewButton = document.querySelector('#previewButton');
const toast = document.querySelector('#toast');
const conversionStatus = document.querySelector('#conversionStatus');
const conversionStatusFoot = document.querySelector('#conversionStatusFoot');
const conversionFailureReason = document.querySelector('#conversionFailureReason');
const currentSource = document.querySelector('#currentSource');
const currentSourceFoot = document.querySelector('#currentSourceFoot');
const outputProfile = document.querySelector('#outputProfile');
const outputProfileFoot = document.querySelector('#outputProfileFoot');
const outputFormat = document.querySelector('#outputFormat');
const layoutSelect = document.querySelector('#layoutSelect');
const useAzureDI = document.querySelector('#useAzureDI');
const runAccessibilityChecks = document.querySelector('#runAccessibilityChecks');
const activityList = document.querySelector('#activityList');
const libraryList = document.querySelector('#libraryList');
const libraryCount = document.querySelector('#libraryCount');
const settingsSaved = document.querySelector('#settingsSaved');
const clearStorageButton = document.querySelector('#clearStorageButton');
const clearLibraryButton = document.querySelector('#clearLibraryButton');
const clearActivityButton = document.querySelector('#clearActivityButton');
const apiBaseUrl = 'http://127.0.0.1:8000';
let selectedFile = null;
const activityStorageKey = 'academian-conversion-activity';
const toggleStorageKey = 'academian-toggle-settings';

function showToast(message) {
  toast.textContent = message;
  toast.classList.add('show');
  window.setTimeout(() => toast.classList.remove('show'), 2800);
}

function setFile(file) {
  if (!file || (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf'))) {
    showToast('Please choose a PDF document.');
    return;
  }
  selectedFile = file;
  conversionFailureReason.hidden = true;
  conversionFailureReason.textContent = '';
  downloadLink.hidden = true;
  dropTitle.textContent = file.name;
  dropText.textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB ready to process`;
  sourceFile.querySelector('strong').textContent = file.name;
  sourceFile.querySelector('span:not(.pdf-badge)').textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB · Added just now`;
  currentSource.textContent = file.name;
  currentSourceFoot.textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB · Ready for conversion`;
  conversionStatus.innerHTML = '<i></i>Ready to convert';
  conversionStatusFoot.textContent = 'Source document selected';
  showToast('Source document added.');
}

function formatTime(timestamp) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(timestamp));
}

function renderActivity() {
  const activities = JSON.parse(localStorage.getItem(activityStorageKey) || '[]');
  if (!activities.length) {
    activityList.innerHTML = '<div class="empty-activity">No conversions yet. Your latest activity will appear here.</div>';
    return;
  }
  activityList.innerHTML = activities.map((activity) => `
    <div class="activity-item">
      <span class="activity-dot ${activity.status === 'failed' ? 'red' : 'green'}"></span>
      <div><strong>${activity.status === 'failed' ? 'Conversion failed' : 'Conversion completed'}</strong><p>${activity.title}${activity.status === 'failed' ? '' : '.epub'}</p><small>${formatTime(activity.createdAt)}${activity.status === 'failed' ? ` · ${activity.error}` : ` · ${activity.pages} page${activity.pages === 1 ? '' : 's'}`}</small></div>
    </div>`).join('');
}

function renderLibrary() {
  const activities = JSON.parse(localStorage.getItem(activityStorageKey) || '[]');
  libraryCount.textContent = `${activities.length} file${activities.length === 1 ? '' : 's'}`;
  if (!activities.length) {
    libraryList.innerHTML = '<div class="empty-state"><strong>Your library is empty</strong><span>Complete a conversion to see downloadable ePub files here.</span></div>';
    return;
  }
  libraryList.innerHTML = activities.map((activity) => `
    <article class="library-card"><span class="epub-badge">ePub</span><div><strong>${activity.title}.epub</strong><span>${activity.pages} page${activity.pages === 1 ? '' : 's'} · ${formatTime(activity.createdAt)}</span></div><div class="library-links"><a href="${activity.downloadUrl || '#'}" ${activity.downloadUrl ? 'download' : ''} aria-label="Download ${activity.title}">Download file</a></div></article>`).join('');
}

function recordConversion(result) {
  const activities = JSON.parse(localStorage.getItem(activityStorageKey) || '[]');
  activities.unshift({ title: result.title, pages: result.pages, createdAt: Date.now() });
  activities[0].downloadUrl = `${apiBaseUrl}${result.download_url}`;
  activities[0].previewUrl = `${apiBaseUrl}/api/v1/previews/${result.conversion_id}`;
  localStorage.setItem(activityStorageKey, JSON.stringify(activities.slice(0, 10)));
  renderActivity();
  renderLibrary();
}

function recordConversionFailure(error) {
  const filename = selectedFile?.name || 'PDF conversion';
  const activities = JSON.parse(localStorage.getItem(activityStorageKey) || '[]');
  activities.unshift({
    title: filename,
    status: 'failed',
    error,
    createdAt: Date.now(),
  });
  localStorage.setItem(activityStorageKey, JSON.stringify(activities.slice(0, 10)));
  renderActivity();
  const failedActivity = activityList.querySelector('.activity-item');
  failedActivity?.classList.add('activity-failed-highlight');
  document.querySelector('#activity').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// Core file-selection and conversion wiring is attached first, and before any code that
// could throw (e.g. a stale page missing an element some other section expects) so the
// "Choose PDF" button and conversion flow keep working even if a later section fails.
browseButton.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', (event) => setFile(event.target.files[0]));
['dragenter', 'dragover'].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
  event.preventDefault();
  dropzone.classList.add('dragging');
}));
['dragleave', 'drop'].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
  event.preventDefault();
  dropzone.classList.remove('dragging');
}));
dropzone.addEventListener('drop', (event) => setFile(event.dataTransfer.files[0]));
dropzone.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ' ') fileInput.click();
});

document.querySelector('.remove-button').addEventListener('click', () => {
  fileInput.value = '';
  selectedFile = null;
  downloadLink.hidden = true;
  dropTitle.textContent = 'Drop your PDF here';
  dropText.textContent = 'or browse files from your computer';
  sourceFile.querySelector('strong').textContent = 'spring-catalogue.pdf';
  sourceFile.querySelector('span:not(.pdf-badge)').textContent = '42.8 MB · Added today';
  currentSource.textContent = 'No PDF selected';
  currentSourceFoot.textContent = 'Choose a source document to begin';
  conversionStatus.innerHTML = '<i></i>Ready to convert';
  conversionStatusFoot.textContent = 'No conversion run yet';
  conversionFailureReason.hidden = true;
  conversionFailureReason.textContent = '';
  showToast('Source document removed.');
});

previewButton.addEventListener('click', () => {
  if (previewButton.dataset.url) window.open(previewButton.dataset.url, '_blank', 'noopener');
});
convertButton.addEventListener('click', () => {
  if (!selectedFile) {
    showToast('Choose a PDF before starting conversion.');
    return;
  }
  convertButton.disabled = true;
  convertButton.textContent = 'Converting PDF...';
  conversionStatus.innerHTML = '<i></i>Conversion in progress';
  conversionStatusFoot.textContent = 'Preserving pages, forms, images, and Read Aloud audio';
  const formData = new FormData();
  formData.append('file', selectedFile);
  formData.append('layout', layoutSelect.value);
  formData.append('use_azure_di', useAzureDI.checked ? 'true' : 'false');
  const configurationCheck = useAzureDI.checked
    ? fetch(`${apiBaseUrl}/api/v1/capabilities`).then((response) => response.json()).then((capabilities) => {
      if (!capabilities.azure_document_intelligence_configured) {
        throw new Error('Azure Document Intelligence is not configured. Set AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY for the gateway.');
      }
    })
    : Promise.resolve();
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), 900000);
  configurationCheck.then(() => fetch(`${apiBaseUrl}/api/v1/conversions`, { method: 'POST', body: formData, signal: controller.signal }))
    .then(async (response) => {
      const result = await response.json();
          if (!response.ok || result.status !== 'completed') throw new Error(result.detail || result.reason || 'Conversion failed.');
      downloadLink.href = `${apiBaseUrl}${result.download_url}`;
      downloadLink.download = `${result.title}.epub`;
      downloadLink.hidden = false;
      previewButton.dataset.url = `${apiBaseUrl}/api/v1/previews/${result.conversion_id}`;
      previewButton.hidden = false;
      conversionStatus.innerHTML = '<i></i>Conversion complete';
      conversionStatusFoot.textContent = `${result.pages} page${result.pages === 1 ? '' : 's'} processed · ${formatTime(Date.now())}`;
      conversionFailureReason.hidden = true;
      conversionFailureReason.textContent = '';
      currentSource.textContent = selectedFile.name;
      currentSourceFoot.textContent = `${(selectedFile.size / 1024 / 1024).toFixed(1)} MB · Converted successfully`;
      outputProfile.textContent = result.layout === 'fixed' ? 'ePub 3.2 fixed' : 'ePub 3.2 reflowable';
      outputProfileFoot.textContent = `${result.azure_document_intelligence ? 'Azure DI + ' : ''}Read Aloud + extracted HTML content`;
      recordConversion(result);
      showToast(`Created ePub from ${result.pages} page(s).`);
    })
    .catch((error) => {
      const message = error.name === 'AbortError' ? 'Conversion timed out. Try a smaller PDF.' : error.message;
      conversionStatus.innerHTML = '<i></i>Conversion failed';
      conversionStatusFoot.textContent = message;
      conversionFailureReason.textContent = `Reason: ${message}`;
      conversionFailureReason.hidden = false;
      recordConversionFailure(message);
      showToast(message);
    })
    .finally(() => {
      window.clearTimeout(timeoutId);
      convertButton.disabled = false;
      convertButton.innerHTML = 'Start conversion <span>-></span>';
    });
});

// Everything below is secondary workspace chrome (nav highlighting, activity/library
// rendering, saved toggles, settings panel). It's wrapped so that a missing element here
// (e.g. from a stale cached page) can't take down the conversion flow wired above.
try {
  function updateOutputProfilePreview() {
    const layoutLabel = layoutSelect.value === 'fixed' ? 'fixed layout' : 'reflowable';
    outputProfile.textContent = `${outputFormat.value} ${layoutLabel}`;
    const extras = [];
    if (useAzureDI.checked) extras.push('Azure DI');
    extras.push('Read Aloud');
    if (runAccessibilityChecks.checked) extras.push('Accessibility checks');
    outputProfileFoot.textContent = extras.join(' + ');
  }

  document.querySelectorAll('.nav-item, .brand').forEach((link) => link.addEventListener('click', () => {
    document.querySelectorAll('.primary-nav .nav-item').forEach((item) => item.classList.toggle('active', item.getAttribute('href') === link.getAttribute('href')));
  }));
  renderActivity();
  renderLibrary();

  const savedToggles = JSON.parse(localStorage.getItem(toggleStorageKey) || '{}');
  if (savedToggles.useAzureDI !== undefined) useAzureDI.checked = savedToggles.useAzureDI;
  if (savedToggles.runAccessibilityChecks !== undefined) runAccessibilityChecks.checked = savedToggles.runAccessibilityChecks;
  [useAzureDI, runAccessibilityChecks].forEach((toggle) => toggle.addEventListener('change', () => {
    localStorage.setItem(toggleStorageKey, JSON.stringify({
      useAzureDI: useAzureDI.checked,
      runAccessibilityChecks: runAccessibilityChecks.checked,
    }));
    updateOutputProfilePreview();
  }));
  [outputFormat, layoutSelect].forEach((control) => control.addEventListener('change', updateOutputProfilePreview));
  updateOutputProfilePreview();

  clearStorageButton.addEventListener('click', () => {
    if (!window.confirm('Clear saved conversions and workspace settings from this browser?')) return;
    localStorage.removeItem(activityStorageKey);
    localStorage.removeItem('academian-settings');
    localStorage.removeItem(toggleStorageKey);
    useAzureDI.checked = false;
    runAccessibilityChecks.checked = true;
    renderActivity();
    renderLibrary();
    updateOutputProfilePreview();
    settingsSaved.textContent = 'Cleared';
    showToast('Local storage cleared.');
    window.setTimeout(() => { settingsSaved.textContent = 'Saved locally'; }, 1800);
  });

  clearLibraryButton.addEventListener('click', async () => {
    const activities = JSON.parse(localStorage.getItem(activityStorageKey) || '[]');
    if (!activities.length) {
      showToast('Library is already empty.');
      return;
    }
    if (!window.confirm('Delete all generated ePub files from the Library?')) return;
    clearLibraryButton.disabled = true;
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/conversions`, { method: 'DELETE' });
      const result = await response.json();
      if (!response.ok || result.status !== 'cleared') throw new Error('Could not clear the Library.');
      localStorage.removeItem(activityStorageKey);
      renderActivity();
      renderLibrary();
      showToast(`Deleted ${result.deleted} generated file${result.deleted === 1 ? '' : 's'}.`);
    } catch (error) {
      showToast(error.message);
    } finally {
      clearLibraryButton.disabled = false;
    }
  });

  clearActivityButton.addEventListener('click', () => {
    const activities = JSON.parse(localStorage.getItem(activityStorageKey) || '[]');
    if (!activities.length) {
      showToast('Recent activity is already empty.');
      return;
    }
    if (!window.confirm('Clear recent activity from this workspace?')) return;
    localStorage.removeItem(activityStorageKey);
    renderActivity();
    renderLibrary();
    showToast('Recent activity cleared.');
  });

  const settingsInputs = ['defaultOutput', 'defaultLayout', 'defaultDirection', 'defaultAccessibility'].map((id) => document.querySelector(`#${id}`));
  const savedSettings = JSON.parse(localStorage.getItem('academian-settings') || '{}');
  settingsInputs.forEach((input) => {
    if (!input) return;
    if (savedSettings[input.id] !== undefined) {
      if (input.type === 'checkbox') input.checked = savedSettings[input.id];
      else input.value = savedSettings[input.id];
    }
    input.addEventListener('change', () => {
      const settings = JSON.parse(localStorage.getItem('academian-settings') || '{}');
      settings[input.id] = input.type === 'checkbox' ? input.checked : input.value;
      localStorage.setItem('academian-settings', JSON.stringify(settings));
      settingsSaved.textContent = 'Saved just now';
      window.setTimeout(() => { settingsSaved.textContent = 'Saved locally'; }, 1800);
    });
  });

  document.querySelector('#inviteButton').addEventListener('click', () => showToast('Invite link copied for your review team.'));
} catch (error) {
  console.error('Academian workspace chrome failed to initialize:', error);
}
