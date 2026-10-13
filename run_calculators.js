#!/usr/bin/env node
/* run_calculators.js - one headless run per calculator against a served site (S117 WP-1, I297).
 *
 * Reads calculators.json (one fixed input and an OUTSIDE-sourced expected result per calculator), opens each
 * page in headless Chrome (puppeteer-core, the runner's preinstalled Chrome or CHROME_PATH), fills the form by
 * element id, submits it with requestSubmit (so the controller's listener and native validation both run), waits
 * for the result container and asserts every expect string is in its text (or expect_pattern matches it).
 * A copy of the per-push functional gate's drive, cut down so this folder can move to the public repo alone:
 * no device profile module, no reset or reach probes. Cross-origin requests are aborted so a third-party embed
 * can never fail the check; the site's own pages, scripts and data load as a reader's do.
 *
 * Usage: node run_calculators.js <base-url> <calculators.json> [--only name[,name]] [--json out.json]
 * Exit:  0 every calculator passed | 1 a calculator failed | 2 usage or launch error
 */
'use strict';
const fs = require('fs');
const path = require('path');
// puppeteer-core is loaded after the config is validated, so the floors below run (and are tested) without it.

const NAV_TIMEOUT = parseInt(process.env.UKFT_LC_NAV_TIMEOUT || '45000', 10);
const SUBMIT_ATTEMPTS = parseInt(process.env.UKFT_LC_SUBMIT_ATTEMPTS || '3', 10);
// A fixture that plants a calculator that never populates sets a short cap, so the deploy step stays in budget.
const RESULT_TIMEOUT_CAP = parseInt(process.env.UKFT_LC_RESULT_TIMEOUT_CAP || '0', 10);

function chromePath() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const fixed = ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser'];
  for (const p of fixed) if (fs.existsSync(p)) return p;
  const pw = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(process.env.HOME || '', '.cache', 'ms-playwright');
  try {
    for (const d of fs.readdirSync(pw)) {
      if (!/^chromium-\d+$/.test(d)) continue;
      const c = path.join(pw, d, 'chrome-linux', 'chrome');
      if (fs.existsSync(c)) return c;
    }
  } catch (_) { /* no playwright cache */ }
  return null;
}

function norm(s) {
  return String(s).replace(/[  ]/g, ' ').replace(/\s+/g, ' ').trim();
}

async function runOne(browser, base, defaults, c) {
  const url = base.replace(/\/$/, '') + c.path;
  const formSel = c.form === null ? null : (c.form || defaults.form || '#tool-form');
  const resultSel = c.result || defaults.result || '#results-container';
  let resultTimeout = parseInt(c.result_timeout || defaults.result_timeout || 8000, 10);
  if (RESULT_TIMEOUT_CAP > 0) resultTimeout = Math.min(resultTimeout, RESULT_TIMEOUT_CAP);
  const page = await browser.newPage();
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  const baseHost = new URL(base).host;
  await page.setRequestInterception(true);
  page.on('request', (req) => {
    let host = '';
    try { host = new URL(req.url()).host; } catch (_) { /* data: urls */ }
    return (host && host !== baseHost) ? req.abort() : req.continue();
  });
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  const problems = [];
  // A dialog (a controller's alert on a calculation error) is a fault, and left open it would hang the run.
  page.on('dialog', (d) => { problems.push('dialog: ' + d.message()); d.dismiss().catch(() => {}); });
  try {
    const resp = await page.goto(url, { waitUntil: 'networkidle2', timeout: NAV_TIMEOUT });
    const status = resp ? resp.status() : 0;
    if (status !== 200) problems.push('HTTP ' + status + ' loading ' + url);
    if (!problems.length && formSel) {
      const missing = await page.evaluate((fill, formSel, steps) => {
        const form = document.querySelector(formSel);
        if (!form) return { noForm: true };
        const miss = [];
        const doFill = (obj) => {
          for (const [id, value] of Object.entries(obj)) {
            const el = document.getElementById(id);
            if (!el) { miss.push(id); continue; }
            const type = (el.getAttribute('type') || '').toLowerCase();
            if (type === 'radio' || type === 'checkbox') {
              el.checked = (value === true || value === 'true' || String(value) === el.value);
            } else {
              el.value = String(value);
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
          }
        };
        doFill(fill);
        for (const s of steps) {
          if (s.click) {
            const el = document.querySelector(s.click);
            if (!el) { miss.push('click:' + s.click); continue; }
            el.click();
          } else if (s.fill) {
            doFill(s.fill);
          }
        }
        return { miss };
      }, c.fill || {}, formSel, c.steps || []);
      if (missing.noForm) problems.push('form ' + formSel + ' not found');
      else if (missing.miss.length) problems.push('field id(s) not on the page: ' + missing.miss.join(', '));
    }
    if (!problems.length) {
      let populated = false;
      for (let attempt = 0; attempt < (formSel ? SUBMIT_ATTEMPTS : 1) && !populated; attempt++) {
        if (formSel) {
          await page.evaluate((formSel) => {
            const form = document.querySelector(formSel);
            if (form.requestSubmit) form.requestSubmit();
            else form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
          }, formSel);
        }
        try {
          await page.waitForFunction((sel, pattern) => {
            const el = document.querySelector(sel);
            if (!el || el.classList.contains('u-hidden')) return false;
            const t = el.textContent.trim();
            return pattern ? new RegExp(pattern).test(t) || t.length > 15 : t.length > 15;
          }, { timeout: resultTimeout }, resultSel, c.expect_pattern || null);
          populated = true;
        } catch (e) { /* submit again */ }
      }
      if (!populated) {
        problems.push('result ' + resultSel + ' did not populate within ' + resultTimeout + ' ms' + (formSel ? ' after ' + SUBMIT_ATTEMPTS + ' submit(s)' : ''));
      } else {
        const got = norm(await page.evaluate((sel) => document.querySelector(sel).textContent, resultSel));
        for (const want of (c.expect || [])) if (!got.includes(norm(want))) problems.push('missing "' + want + '"');
        if (c.expect_pattern && !new RegExp(c.expect_pattern).test(got)) problems.push('result "' + got.slice(0, 80) + '" does not match /' + c.expect_pattern + '/');
        if (problems.length) problems.push('got: ' + (got.length > 300 ? got.slice(0, 300) + '...' : got));
      }
    }
    if (pageErrors.length) problems.push('page error(s): ' + pageErrors.slice(0, 3).join(' | '));
  } catch (e) {
    problems.push('exception: ' + e.message);
  }
  await page.close();
  return problems;
}

(async () => {
  const args = process.argv.slice(2);
  const only = (args.includes('--only') ? args[args.indexOf('--only') + 1] : '').split(',').filter(Boolean);
  const jsonOut = args.includes('--json') ? args[args.indexOf('--json') + 1] : null;
  const positional = args.filter((a, i) => !a.startsWith('--') && !(i > 0 && (args[i - 1] === '--only' || args[i - 1] === '--json')));
  const [base, cfgPath] = positional;
  if (!base || !cfgPath) {
    console.error('usage: run_calculators.js <base-url> <calculators.json> [--only a,b] [--json out.json]');
    process.exit(2);
  }
  const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
  // Floors (audit round 1, finding 9): an empty config or an unknown --only name is a usage error, never "0/0 passed".
  const all = Array.isArray(cfg.calculators) ? cfg.calculators : [];
  if (!all.length) {
    console.error('run_calculators: ' + cfgPath + ' lists no calculator');
    process.exit(2);
  }
  const unknown = only.filter((n) => !all.some((c) => c.calculator === n));
  if (unknown.length) {
    console.error('run_calculators: --only names no configured calculator: ' + unknown.join(', '));
    process.exit(2);
  }
  const todo = all.filter((c) => !only.length || only.includes(c.calculator));
  if (process.env.UKFT_LC_VALIDATE_ONLY) {  // the fixture's floor cases: stop before the browser
    console.log('run_calculators: config valid, ' + todo.length + ' calculator(s) to run');
    process.exit(0);
  }
  const chrome = chromePath();
  if (!chrome) {
    console.error('run_calculators: no Chrome found (set CHROME_PATH)');
    process.exit(2);
  }
  const puppeteer = require('puppeteer-core');
  const browser = await puppeteer.launch({ executablePath: chrome, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const results = [];
  let failed = 0;
  for (const c of todo) {
    const t0 = Date.now();
    const problems = await runOne(browser, base, cfg.defaults || {}, c);
    const ms = Date.now() - t0;
    results.push({ calculator: c.calculator, path: c.path, ok: !problems.length, problems, ms });
    if (problems.length) {
      failed++;
      console.log('FAIL  ' + c.calculator + '  (' + ms + ' ms)');
      problems.forEach((p) => console.log('        ' + p));
    } else {
      console.log('PASS  ' + c.calculator + '  (' + ms + ' ms)');
    }
  }
  await browser.close();
  if (results.length !== todo.length) {
    failed++;
    console.log('FAIL  ' + results.length + ' result(s) for ' + todo.length + ' configured calculator(s)');
  }
  if (jsonOut) fs.writeFileSync(jsonOut, JSON.stringify({ base, chrome, results }, null, 1));
  console.log('calculators: ' + (results.length - Math.min(failed, results.length)) + '/' + todo.length + ' passed');
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error('run_calculators: fatal', e); process.exit(2); });
