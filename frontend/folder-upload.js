const localFolders = new Map();
const folderIgnored = new Set(['.git', '.venv', 'venv', 'node_modules', '__pycache__', '.DS_Store']);

function openFolderPicker() { openFolderPickerFor('i_root'); }
function openFolderPickerFor(inputId, filesOnly = false) {
  if (localFolders.get(inputId)?.upload) { toast('正在上传文件夹，请稍后'); return; }
  const target = document.getElementById(inputId);
  if (!target) return;
  const picker = document.createElement('input');
  picker.type = 'file';
  picker.multiple = true;
  if (!filesOnly) picker.setAttribute('webkitdirectory', '');
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
      const name = filesOnly ? (files.length === 1 ? files[0].name : '所选脚本文件') : files[0].webkitRelativePath.split('/')[0];
      const rawArchive = filesOnly && files.length === 1 && /\.zip$/i.test(files[0].name);
      const scriptCount = files.filter(file => /\.(pyw?|sh|bash|[cm]?js)$/i.test(file.name)).length;
      localFolders.set(inputId, {input:target, files, name, filesOnly, rawArchive});
      target.value = '';
      const label = document.getElementById(inputId + '_label');
      if (label) label.value = `${name} · ${files.length} 个文件（待上传）`;
      const analysis = document.getElementById(inputId + '_analysis');
      if (analysis) analysis.textContent = rawArchive ? 'ZIP 压缩包 · 上传后识别' : `已选 ${files.length} 个文件 · ${scriptCount} 个脚本候选`;
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
    let archive;
    if (selection.rawArchive) archive = selection.files[0];
    else {
      for (const file of selection.files) entries[selection.filesOnly ? `selected-scripts/${file.name}` : file.webkitRelativePath] = new Uint8Array(await file.arrayBuffer());
      archive = await new Promise((resolve,reject) => fflate.zip(entries, {level:1}, (error,data) => error ? reject(error) : resolve(data)));
    }
    const response = await fetch('/api/files/upload-folder' + (selection.rawArchive ? '?archive=true' : ''), {method:'POST', headers:{'Content-Type':'application/zip'}, body:archive});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || (response.status === 401 ? '登录已过期，请重新登录后上传' : '文件夹上传失败，请重试'));
    }
    const result = await response.json();
    target.value = result.path;
    label.value = inputId === 'i_root' ? result.path : `${selection.name} · ${result.script_count} 个脚本`;
    target.dataset.scriptCount = result.script_count;
    target.dataset.runnableCount = result.runnable_count;
    showScriptRecognition(inputId, result);
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

function showScriptRecognition(inputId, result) {
  const analysis = document.getElementById(inputId + '_analysis');
  if (!analysis) return;
  analysis.replaceChildren();
  const summary = document.createElement('div');
  summary.textContent = `已识别 ${result.script_count} 个脚本 · ${result.runnable_count} 个可运行 · ${result.other_count} 个配套文件`;
  analysis.append(summary);
  for (const script of result.scripts.slice(0, 8)) {
    const line = document.createElement('div');
    line.textContent = `${script.name} · ${script.language}${script.reason ? ' · ' + script.reason : ''}`;
    analysis.append(line);
  }
  if (!result.script_count) summary.textContent = '未识别到测试脚本；配套文件已保留';
}
