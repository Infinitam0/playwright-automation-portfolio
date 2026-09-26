// Walk a multi-step quote/intake wizard end to end at a given viewport and report
// what the browser shows per step: HTTP status, H1/H2, horizontal overflow,
// console errors, failed requests, plus a full-page screenshot per step.
// Optionally submits the contact opt-in form with a clearly marked test lead.
// Prints one JSON report to stdout.
//
// Usage: BASE_URL=http://localhost:3000 VIEWPORT=375x667 OPTIN=1 node wizard-filler.mjs [outPrefix]
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { chromium } from 'playwright';

const BASE_URL = process.env.BASE_URL ?? 'http://localhost:3000';
const WIZARD_PATH = process.env.WIZARD_PATH ?? '/quote/';
const [vw, vh] = (process.env.VIEWPORT ?? '375x667').split('x').map(Number);
const OPTIN = process.env.OPTIN === '1';
const outPrefix = process.argv[2] ?? path.join('out', 'wizard');
mkdirSync(path.dirname(outPrefix), { recursive: true });

// ---------------------------------------------------------------------------
// The wizard, as data. Each step runs in order:
//   fill     {selector: value}  type into inputs
//   choose   label text/regex   click the <label> of a radio/checkbox option
//   click    button name regex  press a button by its accessible name
//   waitFor  {heading} | {text} wait for the next screen to render
//   fallback button name regex  escape hatch if waitFor times out (e.g. an
//                               address lookup failed and the site offers
//                               "continue without address")
//   settle   true               also wait for network idle (result screens)
// then a snapshot (screenshot + page info) is taken under `name`.
// The labels below are placeholders: point them at the target wizard's UI.
// ---------------------------------------------------------------------------
const STEPS = [
  {
    name: 'question-1',
    fill: {
      '#postcode': process.env.POSTCODE ?? '1234 AB',
      '#house-number': process.env.HOUSE_NUMBER ?? '1',
    },
    click: /Start/,
    waitFor: { heading: 'What kind of project is it?' },
    fallback: /without (an )?address/i,
  },
  {
    name: 'question-2',
    // ANSWER_1 lets one run exercise a different branch of the result logic.
    choose: new RegExp(`^${process.env.ANSWER_1 ?? 'Renovation'}`),
    click: /^Next/,
    waitFor: { heading: 'How large is the area?' },
  },
  {
    name: 'question-3',
    choose: /^50 – 100 m²$/,
    click: /^Next/,
    waitFor: { heading: 'When would you like to start?' },
  },
  {
    name: 'result',
    choose: /^Within 3 months/,
    click: /See my estimate/,
    waitFor: { heading: /estimate/i },
    settle: true,
  },
];

// Optional contact opt-in shown on the result screen. The test lead is clearly
// marked so it can be filtered out of the CRM on the other side.
const OPTIN_FORM = {
  preChoose: 'Private customer',
  form: /Have a specialist contact you/i,
  fields: [
    [/^Name/, 'E2E Test Lead (staging, safe to delete)'],
    [/^Email/, 'e2e-test-lead@example.com'],
    [/^Postcode/, '1234 AB'],
    [/^House number/, '1'],
  ],
  checkboxes: [/I give permission/i, /I agree/i],
  submit: /contact me/i,
  confirmation: /Thank you, we have received your details/,
};

// Content assertions. They run once, on the result screen's <main> text (plus
// the landing text where noted); the other steps only get the per-step
// snapshot. Each returns a boolean or a small extract.
const RESULT_ASSERTIONS = {
  estimateShown: (t) => /Estimated cost/.test(t),
  twoOptions: (t) => /Basic option/.test(t) && /Premium option/.test(t),
  footnoteDate: (t) => /Last updated \d{2}-\d{2}-\d{4}\./.test(t),
  quoteCta: (t) => /Request a free quote/.test(t),
  // Currency formatters put a no-break space after the symbol.
  priceFormatted: (t) => /€[\s ]\d/.test(t),
  keyLines: (t) => t.split('\n').filter((l) => /€|estimate|weeks/i.test(l)).slice(0, 8),
  noZeroAmounts: (t) => !/€[\s ]0(?![\d,.])/.test(t),
  noEmDash: (t, landing) => !/—/.test(t) && !/—/.test(landing),
};

// ---------------------------------------------------------------------------

const browser = await chromium.launch();
const ctx = await browser.newContext({
  ignoreHTTPSErrors: true, // staging behind a self-signed TLS front
  viewport: { width: vw, height: vh },
  deviceScaleFactor: 2,
  isMobile: vw < 500,
  hasTouch: vw < 500,
  locale: process.env.LOCALE ?? 'en-US',
});
const page = await ctx.newPage();
const consoleErrors = [];
const failed = [];
page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200));
});
page.on('requestfailed', (r) =>
  failed.push(`${r.method()} ${r.url()} ${r.failure()?.errorText ?? ''}`),
);

const steps = [];
const notes = [];
async function snapshot(name) {
  const info = await page.evaluate(() => ({
    h1: document.querySelector('h1')?.textContent?.trim() ?? null,
    h2: document.querySelector('main h2')?.textContent?.trim() ?? null,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
  }));
  const shot = `${outPrefix}-${vw}x${vh}-${steps.length + 1}-${name}.png`;
  await page.screenshot({ path: shot, fullPage: true });
  steps.push({ name, ...info, shot });
}

async function choose(label) {
  await page.locator('label', { hasText: label }).first().click();
}

function waitForScreen(waitFor, timeout) {
  return waitFor.heading
    ? page.getByRole('heading', { name: waitFor.heading }).first().waitFor({ timeout })
    : page.getByText(waitFor.text).first().waitFor({ timeout });
}

async function runStep(step) {
  for (const [selector, value] of Object.entries(step.fill ?? {})) {
    await page.locator(selector).fill(value);
  }
  if (step.choose) await choose(step.choose);
  if (step.click) await page.getByRole('button', { name: step.click }).click();
  if (step.waitFor) {
    try {
      await waitForScreen(step.waitFor, 30_000);
    } catch (e) {
      const escape = step.fallback && page.getByRole('button', { name: step.fallback });
      if (escape && (await escape.isVisible().catch(() => false))) {
        notes.push(`${step.name}: primary path stuck, took fallback`);
        await escape.click();
        await waitForScreen(step.waitFor, 15_000);
      } else {
        await snapshot(`${step.name}-stuck`);
        throw e;
      }
    }
  }
  if (step.settle) await page.waitForLoadState('networkidle');
  await snapshot(step.name);
}

const t0 = Date.now();
const res = await page.goto(`${BASE_URL}${WIZARD_PATH}`, { waitUntil: 'networkidle', timeout: 60_000 });
const status = res?.status();
const meta = await page.evaluate(() => ({
  title: document.title,
  robots: document.querySelector('meta[name="robots"]')?.getAttribute('content') ?? null,
  canonical: document.querySelector('link[rel="canonical"]')?.getAttribute('href') ?? null,
}));
// Cookie banner: pick the most privacy-friendly button if one is shown.
const reject = page.getByRole('button', { name: /Only necessary|Reject|Decline/i }).first();
if (await reject.count()) await reject.click().catch(() => {});
await snapshot('landing');
const landingText = await page.locator('main').innerText();

for (const step of STEPS) await runStep(step);
const msToResult = Date.now() - t0;
const resultText = await page.locator('main').innerText();
// Where focus lands after the result renders (an accessibility check).
const focus = await page.evaluate(() => {
  const a = document.activeElement;
  return a ? `${a.tagName.toLowerCase()}${a.id ? `#${a.id}` : ''}` : 'none';
});

let optin = null;
if (OPTIN) {
  if (OPTIN_FORM.preChoose) await choose(OPTIN_FORM.preChoose);
  const form = page.getByRole('form', { name: OPTIN_FORM.form });
  for (const [label, value] of OPTIN_FORM.fields) await form.getByLabel(label).fill(value);
  for (const name of OPTIN_FORM.checkboxes) await form.getByRole('checkbox', { name }).check();
  await form.getByRole('button', { name: OPTIN_FORM.submit }).click();
  await page.getByText(OPTIN_FORM.confirmation).waitFor({ timeout: 30_000 });
  await snapshot('optin-confirmed');
  optin = 'confirmed';
}

const assertions = Object.fromEntries(
  Object.entries(RESULT_ASSERTIONS).map(([k, fn]) => [k, fn(resultText, landingText)]),
);

console.log(
  JSON.stringify(
    {
      base: BASE_URL,
      viewport: `${vw}x${vh}`,
      status,
      ...meta,
      msToResult,
      focusAfterResult: focus,
      // Address data must never end up in the URL (analytics/referrer leak).
      urlLeaksAddress: /postcode|house/i.test(page.url()),
      steps,
      notes,
      optin,
      resultHeading: steps.find((s) => s.name === 'result')?.h2,
      resultTextRaw: resultText.replace(/\s+/g, ' '),
      assertions,
      consoleErrors,
      failed,
    },
    null,
    2,
  ),
);
await browser.close();
