/* Static shell only. No API, PDFs, documents, identity or resources enter CacheStorage. */
const PREFIX='vocabatron-static-';
const CACHE=PREFIX+'v3';
const ROOT=new URL('./',self.location.href);
const fixed=['./','manifest.webmanifest','icons/mark.svg','icons/icon-192.png','icons/icon-512.png','icons/maskable-512.png','icons/apple-touch-icon.png'];
const staticPath=url=>url.origin===ROOT.origin && url.pathname.startsWith(ROOT.pathname) &&
  (fixed.some(p=>new URL(p,ROOT).pathname===url.pathname) ||
   url.pathname.startsWith(ROOT.pathname+'assets/') || url.pathname.startsWith(ROOT.pathname+'fonts/'));
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(fixed.map(p=>new URL(p,ROOT).href)))));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith(PREFIX)&&k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('message',event=>{if(event.data==='ACTIVATE_UPDATE')self.skipWaiting();});
self.addEventListener('fetch',event=>{
  const url=new URL(event.request.url);
  if(event.request.method!=='GET'||!staticPath(url))return;
  event.respondWith(fetch(event.request).then(response=>{
    if(response.ok && response.type==='basic' && !response.headers.get('Cache-Control')?.includes('no-store')){
      const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));
    }
    return response;
  }).catch(async()=>{const cached=await caches.match(event.request);return cached||Response.error();}));
});
