const fileInput = document.querySelector('#fileInput');
const browseButton = document.querySelector('#browseButton');
const dropzone = document.querySelector('#dropzone');
const dropTitle = document.querySelector('#dropTitle');
const dropText = document.querySelector('#dropText');
const sourceFile = document.querySelector('#sourceFile');
const convertButton = document.querySelector('#convertButton');
const downloadLink = document.querySelector('#downloadLink');
const toast = document.querySelector('#toast');
const apiBaseUrl = 'http://127.0.0.1:8000';
let selectedFile = null;

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
  downloadLink.hidden = true;
  dropTitle.textContent = file.name;
  dropText.textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB ready to process`;
  sourceFile.querySelector('strong').textContent = file.name;
  sourceFile.querySelector('span:not(.pdf-badge)').textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB · Added just now`;
  showToast('Source document added.');
}

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
  showToast('Source document removed.');
});

document.querySelector('#newProjectButton').addEventListener('click', () => showToast('New project flow is ready for wiring to the API gateway.'));
convertButton.addEventListener('click', () => {
  if (!selectedFile) {
    showToast('Choose a PDF before starting conversion.');
    return;
  }
  convertButton.disabled = true;
  convertButton.textContent = 'Preparing conversion...';
  const formData = new FormData();
  formData.append('file', selectedFile);
  fetch(`${apiBaseUrl}/api/v1/conversions`, { method: 'POST', body: formData })
    .then(async (response) => {
      const result = await response.json();
      if (!response.ok || result.status !== 'completed') throw new Error(result.reason || 'Conversion failed.');
      downloadLink.href = `${apiBaseUrl}${result.download_url}`;
      downloadLink.download = `${result.title}.epub`;
      downloadLink.hidden = false;
      document.querySelector('.status-text').innerHTML = '<i></i>Conversion complete';
      showToast(`Created ePub from ${result.pages} page(s).`);
    })
    .catch((error) => showToast(error.message))
    .finally(() => {
      convertButton.disabled = false;
      convertButton.innerHTML = 'Start conversion <span>-></span>';
    });
});
