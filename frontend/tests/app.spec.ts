import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';

test('library, shared preferences, upload and durable task controls',async({page,context},info)=>{
  const external:string[]=[];const errors:string[]=[];
  page.on('request',request=>{if(!request.url().startsWith('https://127.0.0.1:18766/')&&!request.url().startsWith('blob:'))external.push(request.url());});
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('./');await expect(page.getByRole('heading',{name:'Lesson library'})).toBeVisible();
  await expect(page.getByRole('checkbox',{name:/Select Lesson 3/})).toBeVisible();
  await page.screenshot({path:info.outputPath('library.png'),fullPage:true});
  await page.getByRole('checkbox',{name:/Select Lesson 3/}).check();
  await page.getByRole('spinbutton',{name:'Variants per lesson'}).fill('5');
  await page.getByRole('button',{name:'Generate variants →'}).click();
  await expect(page.getByRole('status').filter({hasText:'Request queued'})).toBeVisible();
  await page.getByRole('button',{name:'Activity',exact:true}).click();
  await expect(page.getByText('0 / 5 verified').last()).toBeVisible();
  const task=page.locator('.task').filter({hasText:'0 / 5 verified'}).first();
  await task.getByRole('button',{name:'Pause',exact:true}).click();
  await expect(task.getByRole('button',{name:'Resume',exact:true})).toBeVisible();
  await task.getByRole('button',{name:'Resume',exact:true}).click();
  page.once('dialog',dialog=>dialog.accept());await task.getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(task.getByRole('heading',{name:'Cancelled',exact:true})).toBeVisible();
  await page.screenshot({path:info.outputPath('activity.png'),fullPage:true});
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  const current=await page.getByRole('spinbutton',{name:'Default variants per lesson'}).inputValue();
  const next=Number(current)===7?8:7;
  await page.getByRole('spinbutton',{name:'Default variants per lesson'}).fill(String(next));
  await page.getByRole('combobox',{name:'Appearance'}).selectOption('dark');
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme','dark');
  await page.screenshot({path:info.outputPath('settings-dark.png'),fullPage:true});
  const second=await context.newPage();await second.goto('./');
  await second.getByRole('button',{name:'Settings',exact:true}).click();
  await expect(second.getByRole('spinbutton',{name:'Default variants per lesson'})).toHaveValue(String(next));
  await second.close();
  await page.getByRole('combobox',{name:'Appearance'}).selectOption('light');
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await page.getByRole('button',{name:'Library',exact:true}).first().click();
  const fixture=JSON.parse(readFileSync('../.cache/browser-fixtures/current.json','utf8'));
  await page.getByLabel('Upload PDF documents').setInputFiles(fixture.source);
  await expect(page.getByRole('status').filter({hasText:'already in your library'})).toBeVisible();
  expect(external).toEqual([]);expect(errors).toEqual([]);
});

test('PDF rendering, keyboard, source text and restore',async({page},info)=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('./');await page.getByRole('button',{name:'Open →'}).first().click();
  await page.getByRole('button',{name:'View source text'}).click();
  await expect(page.getByRole('dialog',{name:'Source text'})).toContainText('SYNONYM:');
  await page.keyboard.press('Escape');await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button',{name:'Preview variant 1'}).click();
  await expect(page.getByRole('status').filter({hasText:'Loading preview'})).toHaveCount(0);
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.getByLabel('Crossword page 1')).toBeVisible();
  expect(await page.locator('canvas').evaluate((c:HTMLCanvasElement)=>c.width>0&&c.height>0)).toBe(true);
  await page.getByRole('button',{name:'Next',exact:true}).click();
  await expect(page.getByLabel('Crossword page 2')).toBeVisible();
  await expect(page.getByRole('status').filter({hasText:'Loading preview'})).toHaveCount(0);
  await page.screenshot({path:info.outputPath('pdf-preview.png'),fullPage:true});
  const downloadPromise=page.waitForEvent('download');
  await page.getByRole('dialog').getByRole('link',{name:'Download PDF'}).click();
  const download=await downloadPromise;expect(download.suggestedFilename()).toMatch(/^Variant_001_Lesson_3_Crossword\.pdf$/);
  await page.keyboard.press('Escape');await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button',{name:'Restore exports'}).click();
  await expect(page.getByRole('status').filter({hasText:'restoration queued'})).toBeVisible();
  expect(errors).toEqual([]);
});

test('private responses, manifest and disconnected state',async({page,context,browserName},info)=>{
  await page.goto('./');await expect(page.getByRole('heading',{name:'Lesson library'})).toBeVisible();
  const session=await page.request.get('api/session');expect(session.headers()['cache-control']).toContain('no-store');
  const manifest=await page.request.get('manifest.webmanifest');expect((await manifest.json()).scope).toBe('/vocabatron/');
  const denied=await page.request.get('api/lessons',{headers:{'Tailscale-User-Login':'not-authorized@example.test'}});expect(denied.status()).toBe(403);
  // Chromium's owned test context trusts the ephemeral loopback certificate.
  // Other engines' self-signed service worker trust differs from real HTTPS.
  if(browserName==='chromium'){
    await page.evaluate(()=>navigator.serviceWorker.ready);
    const urls=await page.evaluate(async()=>{const result:string[]=[];for(const key of await caches.keys()){for(const r of await(await caches.open(key)).keys())result.push(r.url);}return result;});
    expect(urls.length).toBeGreaterThan(0);expect(urls.every(url=>!url.includes('/api/')&&!url.endsWith('.pdf'))).toBe(true);
  }
  await context.setOffline(true);await expect(page.getByRole('status').filter({hasText:'You are offline.'})).toBeVisible();
  await page.screenshot({path:info.outputPath('offline.png'),fullPage:true});
  await context.setOffline(false);
});

test('system share cancellation and unsupported-file fallback',async({page})=>{
  await page.addInitScript(()=>{
    Object.defineProperty(navigator,'canShare',{configurable:true,value:()=>true});
    Object.defineProperty(navigator,'share',{configurable:true,value:()=>Promise.reject(new DOMException('User cancelled','AbortError'))});
  });
  await page.goto('./');await page.getByRole('button',{name:'Open →'}).first().click();
  await page.getByRole('button',{name:'Prepare to share',exact:true}).first().click();
  await page.getByRole('button',{name:'Share now',exact:true}).first().click();
  await expect(page.getByRole('button',{name:'Prepare to share',exact:true}).first()).toBeVisible();
  await expect(page.getByText('Sharing could not be completed.',{exact:false})).toHaveCount(0);
  await page.evaluate(()=>Object.defineProperty(navigator,'canShare',{configurable:true,value:()=>false}));
  await page.getByRole('button',{name:'Prepare to share',exact:true}).first().click();
  await expect(page.getByRole('status').filter({hasText:'File sharing is unavailable in this browser. Use Download.'})).toBeVisible();
  await expect(page.getByRole('link',{name:'Download PDF',exact:true}).first()).toBeVisible();
});
