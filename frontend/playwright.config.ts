import {defineConfig} from '@playwright/test';

export default defineConfig({
  testDir:'./tests',fullyParallel:false,workers:1,retries:0,timeout:45000,
  reporter:[['list'],['json',{outputFile:'test-results/report.json'}]],
  use:{baseURL:'https://127.0.0.1:18766/vocabatron/',ignoreHTTPSErrors:true,
    extraHTTPHeaders:{'Tailscale-User-Login':'owner@example.test'},
    screenshot:'only-on-failure',trace:'retain-on-failure',
    launchOptions:{args:['--disable-gpu']}},
  projects:[
    {name:'chromium',use:{browserName:'chromium',viewport:{width:1440,height:1000},launchOptions:{args:['--disable-gpu','--ignore-certificate-errors']}}},
    {name:'firefox',use:{browserName:'firefox',viewport:{width:1440,height:1000},launchOptions:{firefoxUserPrefs:{'layers.acceleration.disabled':true,'gfx.webrender.software':true}}}},
    {name:'webkit',use:{browserName:'webkit',viewport:{width:390,height:844},isMobile:true,hasTouch:true,
      launchOptions:{args:[],executablePath:process.env.VOCABATRON_TEST_WEBKIT_LAUNCHER}}},
  ],
});
