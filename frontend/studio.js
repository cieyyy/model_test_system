/* One theme for the workspace and its sandboxed benchmark views. */
function studioTheme() { return document.documentElement.dataset.theme || 'dark'; }
function studioSendTheme(frame) {
  frame.contentWindow?.postMessage({type:'benchmark:theme', theme:studioTheme()}, '*');
}
function studioRefreshTheme() {
  const light=studioTheme()==='light';
  const button=document.getElementById('themeToggle');
  button.title=button.ariaLabel=light?'切换到深色':'切换到浅色';
  button.innerHTML=`<i data-lucide="${light?'moon':'sun'}" aria-hidden="true"></i>`;
  document.querySelectorAll('iframe[data-benchmark-frame]').forEach(studioSendTheme);
  studioDecorate();
}
function studioToggleTheme() {
  document.documentElement.dataset.theme=studioTheme()==='dark'?'light':'dark';
  try {localStorage.setItem('platformTheme',studioTheme());} catch {}
  studioRefreshTheme();
}
function studioHeading(view) {
  const pages={dashboard:['01','PERFORMANCE','压测展示台'],history:['02','TEST HISTORY','测试记录'],suites:['NEW','MODEL TEST','新建测试'],reports:['03','TEST REPORTS','测试报告']};
  const [index,label,title]=pages[view];
  const date=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  return `<div class="studio-page-heading"><div><div class="studio-eyebrow"><span>${index}</span>${label}</div><h2>${title}<span class="studio-title-dot" aria-hidden="true">.</span></h2></div><div class="studio-date">${date}<span>MODEL TEST WORKSPACE</span></div></div>`;
}
const studioCommandIcons={
  '新建测试':'plus','一键测试全部项目':'list-checks','预览计划':'scan-line','执行计划':'play','运行本地模拟':'play',
  '刷新报告':'refresh-cw','刷新记录':'refresh-cw','查看结果':'arrow-up-right','运行证据':'folder-open','取消测试':'square','保存新版本':'save','恢复保存版本':'rotate-ccw',
  '新增行':'plus','收起':'chevron-up','恢复本机默认值':'rotate-ccw','HTML':'file-code-2','Excel / CSV':'sheet','JSON':'braces',
};
function studioDecorate() {
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
