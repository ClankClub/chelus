# Product site verification

**final result: blocked**

## Evidence and limits

- Source visual truth: `design/reference.webp`, the first generated concept selected by the user. The user subsequently approved dedicated setup, guide and reference routes in the same identity.
- Source dimensions: 941 × 1672 pixels; a conceptual long-page image, not a known CSS viewport or captured app.
- Intended comparison viewport: 1440 × 1024 desktop and 390 × 844 mobile, with additional full-page evidence. No rendered screenshot, CSS size, device density normalization or combined comparison is available.
- Browser-rendered implementation screenshot: unavailable. `sites-preview start` failed because `/tmp/sites-previewd/requests` is unavailable. A directly started server could not be reached by the cloud browser at the prescribed preview address (`ERR_CONNECTION_REFUSED`). Observing the resulting browser error page was rejected by its URL security policy; no workaround or alternate browser was used.
- Consequently, no full-view or focused-region comparison was performed. Desktop/mobile fidelity, actual overflow, keyboard menu behavior, clipboard operation and console errors remain unverified. Source review and HTTP checks are not substitutes for these checks.

## Required fidelity surfaces

- **Fonts/typography:** self-hosted Libre Caslon Display, system serif body, monospace technical text. Source implementation complete; rendered weight, wrapping, scale and fallback behavior are not verified.
- **Spacing/layout:** cream editorial surface, oversized left-aligned hero, right-side photography, fine rules, conversation demo, hardware section. Mobile stacking and documentation sidebar styles implemented; rendered proportions and overflow are not verified.
- **Colors/tokens:** existing `#720e25` burgundy and `#f3eee8` cream preserved in CSS. Actual rendered contrast and state appearance are not verified.
- **Images/assets:** supplied exact repository brand symbol, self-hosted licensed font and Bootstrap icons. Generated front/back phone photography is explicitly illustrative. Subject changed from the concept’s misleading physical-home-button handset to a compact generic older Android with a teardrop notch. Assets inspected individually; their rendered crops and placement are not verified.
- **Copy/content:** source-reviewed against README, config and CLI. No release announcement, local inference, universal portability, shell-sandbox, or Android-survival guarantee. Additional sections and docs routes were explicitly approved. Workdir and scheduler approval scope were corrected following independent code review.

## Completed non-browser verification

- Existing agent suite: **405 tests passed**. Agent runtime untouched.
- Website suite: **9 tests passed** covering all internal routes, assets and fragments at `/` and `/chelus/`, prefix normalization/rejection, source-output separation, reference defaults, progressive-enhancement markup and preview source-change detection.
- Production static build: **9 HTML pages**, including 404. All assets self-hosted.
- Local HTTP integration: root redirects to the project prefix; all eight content routes and primary CSS, JS and hero asset return 200.
- Setup and Telegram shell examples parse with `bash -n`.
- Site JavaScript passes `node --check`. Workflow YAML parses; action release refs were verified via GitHub and pinned to their commit SHAs.
- Independent read-only code review found no Critical/Important defects. Its three minor findings (workdir reference, scheduling approval scope, preview reloading) are resolved.

## Comparison history

No visual QA pass has occurred. Do not report visual parity or mark this report passed until browser evidence is available.

## Remaining checks before launch

1. Restore an authorized reachable browser preview and inspect all routes directly under `/chelus/`.
2. Capture desktop/mobile at the stated viewports, compare source and implementation in the same combined input, and inspect hero/header/type details at readable scale.
3. Exercise menu/escape/focus behavior, skip link, copy buttons, horizontal code scrolling and primary navigation; check console and failed network requests.
4. Fix actionable visual/accessibility findings and recapture. Replace this blocked result with passed only when evidence supports it.
5. Confirm Pages is configured for GitHub Actions. Actual deployment has not run.
