export const BASE=import.meta.env.BASE_URL;
let csrf='';
export function setCsrf(value:string){csrf=value;}
export async function api<T=any>(path:string,options:RequestInit={},retry=true):Promise<T>{
  const method=options.method||'GET';
  const response=await fetch(BASE+'api/'+path,{...options,credentials:'same-origin',cache:'no-store',headers:{
    ...(options.body?{'Content-Type':'application/json'}:{}),
    ...(method!=='GET'&&method!=='HEAD'?{'X-CSRF-Token':csrf}:{}),...options.headers}}).catch(e=>{window.dispatchEvent(new CustomEvent('connection',{detail:false}));throw e;});
  window.dispatchEvent(new CustomEvent('connection',{detail:response.status!==401&&response.status!==403}));
  if(!response.ok){const data=await response.json().catch(()=>({message:'The server could not complete this request.'}));
    if(retry&&path!=='session'&&(response.status===401||data.code==='CSRF_TOKEN')){const session=await api('session',{},false);setCsrf(session.csrf);return api<T>(path,options,false);}
    throw new Error(data.message||data.detail||'Request failed');}
  return response.json();
}
export function upload(files:File[],progress:(sent:number,total:number)=>void):Promise<any>{
  return new Promise((resolve,reject)=>{const data=new FormData();for(const file of files)data.append('files',file);
    const request=new XMLHttpRequest();request.open('POST',BASE+'api/uploads');request.withCredentials=true;request.setRequestHeader('X-CSRF-Token',csrf);
    request.upload.onprogress=e=>progress(e.loaded,e.lengthComputable?e.total:0);
    request.onerror=()=>reject(new Error('Connection lost. Check Activity before uploading again.'));
    request.onload=()=>{let value;try{value=JSON.parse(request.responseText);}catch{reject(new Error('The server returned an unreadable response.'));return;}
      if(request.status>=200&&request.status<300)resolve(value);else reject(new Error(value.message||'Upload failed'));};request.send(data);
  });
}
export type Lesson={id:string;number:number;display_name:string|null;archived:number;word_count:number;variants:number;status:string;content_short_id:string;sources:{name:string;pages:number[]}[]};
export type Artifact={id:string;filename:string;variant_number:number;batch_number:number;crossing_count:number;pages:number;bytes:number;export_status:string;state:string};
export type Task={id:string;kind:string;lesson_id:string;lesson_number:number|null;status:string;stage:string;completed:number;target_type:string;target_count:number|null;pending_variants:number;detail:any;intent:string};
export type Prefs={version:number;default_count:number;theme:'system'|'light'|'dark';background_prepare:boolean;search_slots:number;threads_per_search:number;document_slots:number};
