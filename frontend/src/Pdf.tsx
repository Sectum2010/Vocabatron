import {useEffect,useRef,useState} from 'react';
import {BASE,type Artifact} from './api';
import {Dialog} from './Dialog';
import * as pdfjs from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
pdfjs.GlobalWorkerOptions.workerSrc=workerUrl;

function PdfSheet({artifact,page}:{artifact:Artifact;page:number}){
  const canvas=useRef<HTMLCanvasElement>(null);const holder=useRef<HTMLDivElement>(null);
  const [error,setError]=useState('');const [loading,setLoading]=useState(true);
  useEffect(()=>{let disposed=false;let render:ReturnType<pdfjs.PDFPageProxy['render']>|undefined;
    setLoading(true);setError('');
    const task=pdfjs.getDocument({url:BASE+'api/artifacts/'+artifact.id+'/pdf',withCredentials:true,
      useSystemFonts:false,disableFontFace:false,stopAtErrors:true,disableAutoFetch:true,rangeChunkSize:65536});
    task.promise.then(async document=>{const sheet=await document.getPage(page);if(disposed)return;
      const width=Math.min(holder.current?.clientWidth||600,900);const basic=sheet.getViewport({scale:1});
      const viewport=sheet.getViewport({scale:width/basic.width*Math.min(devicePixelRatio,2)});
      const target=canvas.current!;target.width=viewport.width;target.height=viewport.height;target.style.width='100%';
      render=sheet.render({canvas:target,viewport});await render.promise;if(!disposed)setLoading(false);
    }).catch(e=>{if(!disposed && e.name!=='RenderingCancelledException'){setError('Preview could not be loaded. You can still download the verified PDF.');setLoading(false);}});
    return()=>{disposed=true;render?.cancel();void task.destroy();if(canvas.current){canvas.current.width=0;canvas.current.height=0;}};
  },[artifact.id,page]);
  return <div className="pdf-sheet" ref={holder}>{loading&&<p role="status">Loading preview…</p>}{error&&<p role="alert">{error}</p>}<canvas ref={canvas} aria-label={'Crossword page '+page}/></div>;
}

export function Pdf({artifact,onClose}:{artifact:Artifact;onClose:()=>void}){
  const [page,setPage]=useState(1);const [both,setBoth]=useState(false);
  return <Dialog title={'PDF preview · Variant '+String(artifact.variant_number).padStart(3,'0')} close={onClose}>
    <div className="pdf-controls"><button disabled={both||page===1} onClick={()=>setPage(1)}>Previous</button><span>{both?'Both pages':'Page '+page+' of 2'}</span><button disabled={both||page===2} onClick={()=>setPage(2)}>Next</button><button className="desktop-preview" onClick={()=>setBoth(!both)}>{both?'Single page':'Show both pages'}</button><a className="button primary" href={BASE+'api/artifacts/'+artifact.id+'/pdf?download=1'}>Download PDF</a></div>
    <div className={both?'pdf-spread':''}><PdfSheet artifact={artifact} page={both?1:page}/>{both&&<PdfSheet artifact={artifact} page={2}/>}</div>
  </Dialog>;
}

export function Share({artifact,notify}:{artifact:Artifact;notify:(text:string)=>void}){
  const [file,setFile]=useState<File|null>(null);const [busy,setBusy]=useState(false);
  const url=BASE+'api/artifacts/'+artifact.id+'/pdf?download=1';
  if(!navigator.share||!navigator.canShare)return <a className="button subtle" href={url}>Download</a>;
  const prepare=async()=>{setBusy(true);try{const response=await fetch(url,{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw new Error();
    const ready=new File([await response.blob()],artifact.filename,{type:'application/pdf'});
    if(navigator.canShare({files:[ready]}))setFile(ready);else{notify('File sharing is unavailable in this browser. Use Download.');}
  }catch{notify('The file could not be prepared. Please try Download.');}finally{setBusy(false);}};
  const share=()=>{if(!file)return;void navigator.share({files:[file],title:artifact.filename}).then(()=>setFile(null)).catch(e=>{if(e.name!=='AbortError')notify('Sharing could not be completed. The download is still available.');setFile(null);});};
  return <button className="subtle" disabled={busy} onClick={file?share:prepare}>{busy?'Preparing…':file?'Share now':'Prepare to share'}</button>;
}
