const localFolders = new Map();
const folderIgnored = new Set(['.git', '.venv', 'venv', 'node_modules', '__pycache__', '.DS_Store']);

function openFolderPicker() { openFolderPickerFor('i_root'); }
function openFolderPickerFor(inputId) {
  if (localFolders.get(inputId)?.upload) { toast('正在上传文件夹，请稍后'); return; }
  const target = document.getElementById(inputId);
  if (!target) return;
  const picker = document.createElement('input');
  picker.type = 'file';
  picker.multiple = true;
  picker.setAttribute('webkitdirectory', '');
  picker.hidden = true;
  document.body.appendChild(picker);
  picker.addEventListener('cancel', () => picker.remove(), {once:true});
  picker.addEventListener('change', async () => {
    try {
      if (!target.isConnected) return;
      const files = Array.from(picker.files).filter(file => !file.webkitRelativePath.split('/').slice(1).some(part => folderIgnored.has(part)));
      if (!files.length) { toast('所选文件夹中没有可上传的文件'); return; }
      if (files.length > 2000 || files.some(file => file.size > 32 * 1048576) || files.reduce((sum,file) => sum + file.size, 0) > 100 * 1048576) {
        toast('文件夹限 2000 个文件、合计 100 MB，单文件不超过 32 MB', 5000); return;
      }
      const name = files[0].webkitRelativePath.split('/')[0];
      localFolders.set(inputId, {input:target, files, name});
      target.value = '';
      const label = document.getElementById(inputId + '_label');
      if (label) label.value = `${name} · ${files.length} 个文件（待上传）`;
      if (inputId === 'i_root') {
        target.value = name;
        state.legacyRoot = await uploadSelectedFolder(inputId);
        toast('文件夹已上传');
        await renderImport();
      }
    } catch (error) { toast(error.message, 5000); }
    finally { picker.remove(); }
  }, {once:true});
  picker.click();
}

async function uploadSelectedFolder(inputId) {
  const target = document.getElementById(inputId);
  const selection = localFolders.get(inputId);
  if (!target || !selection || selection.input !== target) return target?.value || '';
  if (selection.upload) return selection.upload;
  selection.upload = (async () => {
    const label = document.getElementById(inputId + '_label') || target;
    label.value = `${selection.name} · 正在上传…`;
    const entries = Object.create(null);
    for (const file of selection.files) entries[file.webkitRelativePath] = new Uint8Array(await file.arrayBuffer());
    const archive = await new Promise((resolve,reject) => fflate.zip(entries, {level:1}, (error,data) => error ? reject(error) : resolve(data)));
    const response = await fetch('/api/files/upload-folder', {method:'POST', headers:{'Content-Type':'application/zip'}, body:archive});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || (response.status === 401 ? '登录已过期，请重新登录后上传' : '文件夹上传失败，请重试'));
    }
    const result = await response.json();
    target.value = result.path;
    label.value = inputId === 'i_root' ? result.path : `${result.name} · ${result.file_count} 个文件`;
    localFolders.delete(inputId);
    return result.path;
  })();
  try { return await selection.upload; }
  catch (error) {
    selection.upload = null;
    const label = document.getElementById(inputId + '_label');
    if (label) label.value = `${selection.name} · 上传失败，可重试`;
    throw error;
  }
}
