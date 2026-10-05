import {icon,esc,toast} from './components/ui.js';
import {dashboard} from './pages/dashboard.js';
import {prospects,detail} from './pages/prospects.js';
import {outreach} from './pages/outreach.js';
import {strategies,feedback} from './pages/relationships.js';
const pages={dashboard,prospects,outreach,strategies,feedback};
const names={dashboard:'Dashboard',prospects:'Prospects',outreach:'Outreach',strategies:'Strategies',feedback:'Feedback'};
let revision=0;
async function render(quiet=false){
 const saved=quiet?Object.fromEntries([...document.querySelectorAll("#search,#filter,#sort")].map(e=>[e.id,e.value])):{};
 const turn=++revision,[path,query='']=(location.hash.slice(1)||'/dashboard').split('?'),parts=path.split('/').filter(Boolean),page=parts[0]||'dashboard';
 document.querySelector('#navigation').innerHTML=Object.entries(names).map(([key,label])=>`<a href="#/${key}" class="${page===key?'active':''}" ${page===key?'aria-current="page"':''}>${icon(key)}${label}</a>`).join('');
 document.querySelector('#breadcrumb').textContent=names[page]||'Workspace';
 document.title=`Rawaj · ${names[page]||'Agency'}`;
 const main=document.querySelector('#main');if(!quiet)main.innerHTML='<div class="loading" role="status"><div class="spinner"></div>Loading your workspace…</div>';
 try{
 const result=page==='prospects'&&/^\d+$/.test(parts[1])?await detail(parts[1]):await (pages[page]||dashboard)(new URLSearchParams(query));
 if(turn!==revision)return;
 if(quiet&&(window.agencyBusy||document.querySelector("#dialog").open||document.querySelector("#approve")||/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName||"")))return;
 // A quiet refresh rebuilds the page, so keep expanded sections open and scrolled lists where they were.
 const open=quiet?[...main.querySelectorAll('details')].map(d=>d.open):[],scrolls=quiet?[...main.querySelectorAll('.gap-list')].map(e=>e.scrollTop):[];
 main.innerHTML=result.html;result.bind?.();
 main.querySelectorAll('details').forEach((d,i)=>{if(open[i])d.open=true;});
 main.querySelectorAll('.gap-list').forEach((e,i)=>{if(scrolls[i])e.scrollTop=scrolls[i];});
 for(const [id,value] of Object.entries(saved)){const el=document.getElementById(id);if(el){el.value=value;el.dispatchEvent(new Event(id==='search'?'input':'change'));}}
 document.querySelector('#sync-label').textContent='Updated '+new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
 }catch(e){if(turn!==revision)return;if(quiet){document.querySelector('#sync-label').textContent='Update failed — use Refresh to retry';return;}main.innerHTML=`<div class="notice error" role="alert"><strong>We couldn’t load this view.</strong><p>${esc(e.message)}</p><button class="button secondary" id="retry" style="margin-top:15px">Try again</button></div>`;document.querySelector('#retry').onclick=()=>render();}
}
window.addEventListener('hashchange',()=>{document.querySelector('#dialog').close();render();window.scrollTo(0,0);});
window.addEventListener('agency-refresh',()=>render());
document.querySelector('#refresh').onclick=()=>render();
document.querySelector('.skip').onclick=e=>{e.preventDefault();document.querySelector('#main').focus();};
// Refresh visibility-sensitive data when returning to the workspace, without interrupting a review.
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!document.querySelector('#dialog').open)document.querySelector('#sync-label').textContent='Refresh for the latest activity';});
render();

// Keep lifecycle progress current without replacing an active review or form.
setInterval(()=>{if(!document.hidden&&!window.agencyBusy&&!document.querySelector("#dialog").open&&!document.querySelector("#approve")&&!/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName||""))render(true);},10000);
