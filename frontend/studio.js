/* One theme for the workspace and its sandboxed benchmark views. */
function studioTheme() { return document.documentElement.dataset.theme || 'dark'; }
function studioSendTheme(frame) {
  frame.contentWindow?.postMessage({type:'benchmark:theme', theme:studioTheme()}, '*');
}
function studioRefreshTheme() {
  document.getElementById('sessionLogout').style.display=location.protocol==='https:'?'':'none';
  const light=studioTheme()==='light';
  const button=document.getElementById('themeToggle');
  button.title=button.ariaLabel=light?'切换到深色':'切换到浅色';
  button.innerHTML=`<i data-lucide="${light?'moon':'sun'}" aria-hidden="true"></i>`;
  document.querySelectorAll('iframe[data-benchmark-frame]').forEach(studioSendTheme);
  studioDecorate();
}
async function studioLogout() {
  try {
    const response=await fetch('/auth/logout',{method:'POST'});
    if(!response.ok)throw new Error();
    location.replace('/login');
  } catch { toast('退出登录失败，请重试'); }
}
function studioToggleTheme() {
  document.documentElement.dataset.theme=studioTheme()==='dark'?'light':'dark';
  try {localStorage.setItem('platformTheme',studioTheme());} catch {}
  studioRefreshTheme();
}
function studioHeading(view) {
  const titles={dashboard:'数据展示',history:'测试记录',suites:'新建测试',reports:'测试报告'};
  return `<div class="studio-page-heading"><h2>${titles[view]}</h2></div>`;
}
const studioCommandIcons={
  '新建测试':'plus','一键测试全部项目':'list-checks','预览计划':'scan-line','执行计划':'play','运行本地模拟':'play',
  '刷新报告':'refresh-cw','刷新记录':'refresh-cw','查看结果':'arrow-up-right','运行证据':'folder-open','取消测试':'square','保存新版本':'save','恢复保存版本':'rotate-ccw',
  '新增行':'plus','查看报告':'eye','收起':'chevron-up','收起报告':'chevron-up','恢复本机默认值':'rotate-ccw','HTML':'file-code-2','Excel / CSV':'sheet','JSON':'braces',
};
function studioDecorate() {
  document.querySelectorAll('div.empty:not([data-studio-empty])').forEach(empty=>{
    if(empty.closest('td,th'))return;
    empty.dataset.studioEmpty='true';
    empty.classList.add('studio-empty');
    const text=document.createElement('p');
    text.append(...empty.childNodes);
    const icon=document.createElement('span');
    icon.className='studio-empty-icon';
    const view=typeof state==='undefined'?'':state.tab;
    const symbol=({dashboard:'chart-no-axes-combined',matrix:'git-compare-arrows',reports:'file-chart-column',history:'history'})[view]||'inbox';
    icon.innerHTML=`<i data-lucide="${symbol}" aria-hidden="true"></i>`;
    empty.append(icon,text);
    if(empty.parentElement?.id==='workspaceReport'||(empty.parentElement?.id==='historyRecords'&&!workspaceHistoryRuns.length)) {
      const button=document.createElement('button');
      button.className='btn primary';
      button.type='button';
      button.textContent='新建测试';
      button.onclick=()=>go('suites');
      empty.append(button);
    }
  });
  document.querySelectorAll('.suite-config-icon:not([data-studio-icon])').forEach(button=>{
    const icon=button.title.startsWith('配置 ')?'sliders-horizontal':({'恢复原始行':'rotate-ccw','上移':'arrow-up','下移':'arrow-down'}[button.title]);
    if(!icon)return;
    button.dataset.studioIcon='true';
    button.innerHTML=`<i data-lucide="${icon}" aria-hidden="true"></i>`;
  });
  document.querySelectorAll('.btn:not([data-studio-icon])').forEach(button=>{
    const icon=studioCommandIcons[button.textContent.trim()];
    if(!icon)return;
    button.dataset.studioIcon='true';
    if(!button.querySelector('svg,i[data-lucide]')) {
      const element=document.createElement('i');element.dataset.lucide=icon;element.setAttribute('aria-hidden','true');button.prepend(element);
    }
  });
  if(document.querySelector('i[data-lucide]'))window.lucide?.createIcons({attrs:{'stroke-width':1.65,'aria-hidden':'true'}});
}
let studioDecorationFrame=0;
new MutationObserver(()=>{
  cancelAnimationFrame(studioDecorationFrame);
  studioDecorationFrame=requestAnimationFrame(studioDecorate);
}).observe(document.body,{childList:true,subtree:true});
window.addEventListener('DOMContentLoaded',studioRefreshTheme);
