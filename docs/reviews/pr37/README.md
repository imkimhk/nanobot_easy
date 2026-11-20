PR #37: skills screen visual verification (2026-11-17)

The skills screen now uses the shared radius and typography tokens, with smaller dialog shadows. The new-skill dialog keeps its footer visible while the body scrolls, and both skill dialogs position their close button inside the dialog. Explicit `sm:rounded-xl` prevents the shared dialog's `sm:rounded-lg` from overriding the intended 11 px corners.

The branch was updated against `main` at `f55aecbaf1c59c24a89da70bbc10518286085127` (including PR #43). The conflict was resolved with the selection count, applied usage count, trace phase and application evidence preserved.

Verification method

- Real Chromium 156.0.8078.4 rendered the actual before/after React components and shared UI primitives, bundled with Vite in production mode and the project Tailwind configuration. The before component is from the main commit above; the after component is this PR.
- A standalone component harness supplied fixed, synthetic skill records and intercepted every component API request. It used no gateway, private database, real conversations, credentials or external resources. The harness and its build output were removed from the source tree after capture.
- Desktop viewport: 1280 × 577. Mobile viewport: 390 × 844. Device scale: 1. Both light and dark document classes were verified. Finite animations were finished before measurement/capture.
- The selected skill, filter and per-view scroll action were identical for each before/after pair. Registry and catalog captures are full-page captures; dialog and sheet captures show the viewport. The trace captures scroll the actual details region to the applied trace.
- All 48 captures had identical rendered before/after text and no page-level horizontal overflow. The final capture run had no browser page errors or unexpected network requests. After dialogs have 11 px corners and their Cancel/Close/Save actions fit within the dialog and viewport; the New skill button has 7 px corners.
- The manual-entry form was additionally opened in all four viewport/theme combinations. Cancel, Create draft and Close remained within the dialog, and Cancel closed it. No draft was composed or registered; the major-update confirmation used a synthetic dry-run assessment and was cancelled.
- The images were inspected for light/dark readability, boundaries, clipping, radius and shadow changes. All visible records are synthetic and can be published.
- Source ASTs match main after removing JSX `className` attributes. Only presentation changed; the latest skill accounting and all action handlers remain intact.
- Final source checks: WebUI 511 tests across 33 files passed; TypeScript/Vite production build, full ESLint and `git diff --check` passed. Existing nonfatal Browserslist, ambiguous-duration utility, bundle-size and test-environment warnings remain.

The earlier 1280 × 577 baseline clipped the new-skill footer and placed both dialog close buttons against the outer overlay. The after captures demonstrate both fixes. Existing narrow-screen detail-title truncation and internal details scrolling remain visible in the before/after registry captures. This review covers these viewports in Chromium; it does not establish cross-browser coverage or a full responsive redesign.

Screenshot pairs

| Viewport | View | Light before | Light after | Dark before | Dark after |
| --- | --- | --- | --- | --- | --- |
| desktop | Registry and selection | [before](screenshots/desktop-light-registry-before.png) | [after](screenshots/desktop-light-registry-after.png) | [before](screenshots/desktop-dark-registry-before.png) | [after](screenshots/desktop-dark-registry-after.png) |
| desktop | Applied trace after scrolling | [before](screenshots/desktop-light-registry-trace-before.png) | [after](screenshots/desktop-light-registry-trace-after.png) | [before](screenshots/desktop-dark-registry-trace-before.png) | [after](screenshots/desktop-dark-registry-trace-after.png) |
| desktop | New skill dialog | [before](screenshots/desktop-light-new-skill-before.png) | [after](screenshots/desktop-light-new-skill-after.png) | [before](screenshots/desktop-dark-new-skill-before.png) | [after](screenshots/desktop-dark-new-skill-after.png) |
| desktop | Major-update confirmation | [before](screenshots/desktop-light-major-update-before.png) | [after](screenshots/desktop-light-major-update-after.png) | [before](screenshots/desktop-dark-major-update-before.png) | [after](screenshots/desktop-dark-major-update-after.png) |
| desktop | Read-only catalog | [before](screenshots/desktop-light-readonly-catalog-before.png) | [after](screenshots/desktop-light-readonly-catalog-after.png) | [before](screenshots/desktop-dark-readonly-catalog-before.png) | [after](screenshots/desktop-dark-readonly-catalog-after.png) |
| desktop | Read-only detail sheet | [before](screenshots/desktop-light-readonly-detail-before.png) | [after](screenshots/desktop-light-readonly-detail-after.png) | [before](screenshots/desktop-dark-readonly-detail-before.png) | [after](screenshots/desktop-dark-readonly-detail-after.png) |
| mobile | Registry and selection | [before](screenshots/mobile-light-registry-before.png) | [after](screenshots/mobile-light-registry-after.png) | [before](screenshots/mobile-dark-registry-before.png) | [after](screenshots/mobile-dark-registry-after.png) |
| mobile | Applied trace after scrolling | [before](screenshots/mobile-light-registry-trace-before.png) | [after](screenshots/mobile-light-registry-trace-after.png) | [before](screenshots/mobile-dark-registry-trace-before.png) | [after](screenshots/mobile-dark-registry-trace-after.png) |
| mobile | New skill dialog | [before](screenshots/mobile-light-new-skill-before.png) | [after](screenshots/mobile-light-new-skill-after.png) | [before](screenshots/mobile-dark-new-skill-before.png) | [after](screenshots/mobile-dark-new-skill-after.png) |
| mobile | Major-update confirmation | [before](screenshots/mobile-light-major-update-before.png) | [after](screenshots/mobile-light-major-update-after.png) | [before](screenshots/mobile-dark-major-update-before.png) | [after](screenshots/mobile-dark-major-update-after.png) |
| mobile | Read-only catalog | [before](screenshots/mobile-light-readonly-catalog-before.png) | [after](screenshots/mobile-light-readonly-catalog-after.png) | [before](screenshots/mobile-dark-readonly-catalog-before.png) | [after](screenshots/mobile-dark-readonly-catalog-after.png) |
| mobile | Read-only detail sheet | [before](screenshots/mobile-light-readonly-detail-before.png) | [after](screenshots/mobile-light-readonly-detail-after.png) | [before](screenshots/mobile-dark-readonly-detail-before.png) | [after](screenshots/mobile-dark-readonly-detail-after.png) |
