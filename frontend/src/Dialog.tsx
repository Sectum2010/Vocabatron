import {useEffect,useRef,type ReactNode} from 'react';

export function Dialog({title,close,children}:{title:string;close:()=>void;children:ReactNode}){
  const dialog=useRef<HTMLElement>(null);const callback=useRef(close);callback.current=close;
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null;const overflow=document.body.style.overflow;
    document.body.style.overflow='hidden';
    const handler=(e:KeyboardEvent)=>{
      if(e.key==='Escape')callback.current();
      if(e.key==='Tab'){
        const targets=Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),select,textarea,[tabindex="0"]')||[]);
        const first=targets[0],last=targets.at(-1);
        if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}
        else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}
      }
    };
    window.addEventListener('keydown',handler);
    return()=>{window.removeEventListener('keydown',handler);document.body.style.overflow=overflow;previous?.focus();};
  },[]);
  return <div className="modal-backdrop" onClick={close}><section ref={dialog} className="pdf-modal" role="dialog" aria-modal="true" aria-label={title} onClick={e=>e.stopPropagation()}>
    <header><h2>{title}</h2><button autoFocus onClick={close} aria-label={'Close '+title.toLowerCase()}>×</button></header><div className="dialog-body">{children}</div>
  </section></div>;
}
