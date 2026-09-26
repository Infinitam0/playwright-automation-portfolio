# Multi-step wizard form-filler

## Problem

A website runs a multi-step quote/intake wizard: an address step, a few multiple-choice questions, a calculated estimate, and an optional "have someone contact me" form. Every release needed the same walk-through on mobile and desktop to check that each screen renders, that nothing overflows horizontally, that the result screen shows the right content, and that a submitted lead actually reaches the backend.

## How it works

1. Open the wizard at a chosen viewport (mobile gets touch and `isMobile` emulation) and dismiss the cookie banner with the most privacy-friendly option.
2. Run the steps in the `STEPS` data array. Each step can fill text inputs, click a radio or checkbox option by its label, press a button by accessible name, and wait for the next screen's heading or text.
3. If a step gets stuck (for example the address lookup fails), take the step's `fallback` button when the site shows one. If there is none, save a `-stuck` screenshot and fail.
4. After every step, record H1/H2 and horizontal overflow and save a full-page screenshot. This per-step snapshot is the only check on intermediate screens.
5. Once, on the result screen, run the content assertions in `RESULT_ASSERTIONS` against the visible text. Also record where keyboard focus landed and whether address data leaked into the URL.
6. With `OPTIN=1`, fill and submit the contact form with a clearly labelled test lead, then wait for the confirmation message.
7. Print a single JSON report: status, meta tags, timing, per-step info, assertions, console errors and failed requests.

## Playwright techniques

- Device emulation per run: `viewport`, `deviceScaleFactor`, `isMobile`, `hasTouch`, `locale`, and `ignoreHTTPSErrors` for a staging build behind a self-signed TLS front.
- Accessibility-first locators: `getByRole('button' | 'heading' | 'form' | 'checkbox', { name })`, `getByLabel`, `getByText`. The script never depends on CSS class names.
- Radio and checkbox answers are picked by clicking their `<label>` (`locator('label', { hasText })`), the way a user would.
- Explicit waits on the next screen's heading instead of fixed sleeps, plus `waitForLoadState('networkidle')` for the computed result.
- Fallback when a wait times out: try an escape-hatch button, otherwise save a diagnostic screenshot and rethrow.
- Event listeners collect `console` errors and `requestfailed` requests for the whole session.
- `page.evaluate` reads layout facts (`scrollWidth > clientWidth` for horizontal overflow, `document.activeElement` for focus management) and SEO meta (robots, canonical).
- Full-page screenshots named by viewport and step number.
- Result-screen assertions are data (a map of predicates over the result text), so adapting to another wizard means editing data rather than code.

The step runner handles text inputs, radio/checkbox labels and buttons. It has no `<select>` dropdown step; a wizard that needs one would need a `selectOption` step added.

## How AI was used

I built this with Claude Code while building a site with a multi-step intake wizard. The source project has a `CLAUDE.md` with project rules, and its history has many `Co-Authored-By: Claude` commits. The script came out of the usual loop: plan the check, have Claude write it, run it against staging at mobile and desktop sizes, then fix the script or the site based on what the screenshots and JSON showed. For this portfolio version, Claude generalized the original walk into the `STEPS` / `RESULT_ASSERTIONS` data and stripped site-specific details. I then ran it end to end against a local mock wizard, covering both the main path and the fallback path.

## Build time

Written in a single pass inside a larger QA session: the file was created and last saved within the same minute. It took **minutes**, not hours.

## Run it

```bash
npm install
npx playwright install chromium

# mobile walk, no form submission
BASE_URL=http://localhost:3000 node wizard-filler.mjs out/wizard > report.json

# desktop walk that also submits the opt-in form with the test lead
BASE_URL=https://staging.example.com VIEWPORT=1440x900 OPTIN=1 node wizard-filler.mjs out/wizard > report.json
```

| Env var | Default | Meaning |
|---|---|---|
| `BASE_URL` | `http://localhost:3000` | Site origin |
| `WIZARD_PATH` | `/quote/` | Path of the wizard page |
| `VIEWPORT` | `375x667` | `WIDTHxHEIGHT`; widths under 500 also emulate a touch phone |
| `OPTIN` | unset | `1` submits the contact form with the test lead |
| `POSTCODE`, `HOUSE_NUMBER` | `1234 AB`, `1` | Address used in the first step |
| `ANSWER_1` | `Renovation` | Label prefix of the first answer, for testing another branch |
| `LOCALE` | `en-US` | Browser locale |

Screenshots go to the `outPrefix` argument (default `out/wizard`). The labels in `STEPS` and `OPTIN_FORM` are placeholders; change them to match the target wizard's UI text.

## Files

| File | Description |
|---|---|
| `wizard-filler.mjs` | The walker: step data, assertions, browser setup, per-step snapshot, JSON report |
| `package.json` | Single dependency (`playwright`) and a `walk` script |
