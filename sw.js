// Network-first: always show the newest app version when the server is reachable,
// fall back to the cached shell when offline.
const CACHE='marktradar-shell-v6';const FILES=['./','./index.html','./manifest.webmanifest','./icon.svg'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(FILES)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{
  const url=new URL(e.request.url);
  if(e.request.method!=='GET'||url.origin!==location.origin)return;
  if(url.pathname.includes('/api/')){e.respondWith(fetch(e.request).catch(()=>new Response(JSON.stringify({mode:'unavailable'}),{status:503,headers:{'Content-Type':'application/json'}})));return}
  e.respondWith(fetch(e.request).then(res=>{if(res.ok){const copy=res.clone();caches.open(CACHE).then(c=>c.put(e.request,copy))}return res}).catch(()=>caches.match(e.request).then(r=>r||caches.match('./index.html'))));
});
self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(all=>{for(const client of all){if('focus'in client)return client.focus()}return clients.openWindow('./')}))});
