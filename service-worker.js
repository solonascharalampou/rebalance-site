const CACHE='rebalance-customer-v11';
const SHELL=['./','./index.html','./rebalance-studio-SOURCE.html','./manifest.webmanifest','./app-icon.svg','./app-icon-180.png','./app-icon-512.png'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
  if(event.request.method!=='GET'||new URL(event.request.url).origin!==self.location.origin)return;
  event.respondWith(fetch(event.request).then(response=>{const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));return response;}).catch(()=>caches.match(event.request).then(response=>response||caches.match('./index.html'))));
});
self.addEventListener('push',event=>{
  let data={};try{data=event.data?event.data.json():{};}catch(e){data={body:event.data?event.data.text():''};}
  event.waitUntil(self.registration.showNotification(data.title||'Rebalance',{body:data.body||'You have a session update.',icon:'./app-icon-180.png',badge:'./app-icon-180.png',data:{url:data.url||'./?app=customer'}}));
});
self.addEventListener('notificationclick',event=>{event.notification.close();event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(list=>{for(const client of list){if('focus' in client)return client.focus();}return clients.openWindow(event.notification.data?.url||'./?app=customer');}));});
