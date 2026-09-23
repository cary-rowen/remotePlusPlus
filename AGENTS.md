# NVDA add-on development instructions

Keep changes small, compatible with the add-on's supported NVDA versions, and consistent with nearby code. Do not read or modify unrelated add-ons.

## Working assumptions

- This is an NVDA add-on. The add-on's `buildVars.py`, `pyproject.toml`, `sconstruct`, and nearby code define the actual supported versions and commands.
- Windows PowerShell is the shell. Use tabs in Python, and follow the local Ruff configuration rather than inventing formatting rules.
- Prefer existing helpers, NVDA APIs, and the standard library. Fix the shared root cause and avoid unrelated cleanup, speculative abstractions, and broad rewrites.

## Before editing

1. Inspect the affected files, their callers, relevant tests, and the build configuration. Search with `rg` before adding a helper or changing an API.
2. Read `buildVars.py`, `readme.md`, or `changelog.md` only when packaging, documented behavior, or release notes are affected.
3. Check the add-on's supported NVDA/Python versions before using newer APIs. Preserve compatibility unless the task explicitly changes it.
4. Use the NVDA source and documentation below only for the question at hand. Read the implementation and at least one existing caller before relying on an unfamiliar API.

## Which NVDA references to read

| Task | Read first | Then inspect |
| --- | --- | --- |
| General architecture or thread model | `D:\git\nvda\projectDocs\design\technicalDesignOverview.md` | The matching module under `D:\git\nvda\source` |
| Python style, naming, imports, logging, comments | `D:\git\nvda\projectDocs\dev\codingStandards.md` | Nearby NVDA code and this repository's `pyproject.toml` |
| Public API or add-on lifecycle | `D:\git\nvda\projectDocs\dev\developerGuide\developerGuide.md` | The API definition and callers in `D:\git\nvda\source` |
| Global/app/module plugin | Matching modules under `source/globalPlugins` or `source/appModules` | A comparable built-in plugin and the add-on's registration path |
| Gestures, scripts, input help, keyboard handling | `source/inputCore`, `source/globalCommands`, `source/keyboardHandler` | Gesture resolution and a comparable script declaration |
| Events, focus, UIA, virtual buffers | `source/eventHandler`, `source/NVDAObjects`, `source/UIAHandler`, `source/virtualBuffers` | The event dispatch path and an existing consumer |
| Speech sequences, cancellation, tones, sounds | `source/speech`, `source/speech/commands.py`, and relevant sound modules | The caller and queue/cancellation behavior |
| Braille | `source/braille`, `source/brailleDisplayDrivers` | A driver with the same capability or protocol |
| Settings, dialogs, config persistence | `source/gui`, `source/config`, `source/gui/settingsDialogs.py` | A similar settings panel and its config spec |
| Translation and user-facing strings | `source/locale`, add-on `addon/locale`, `sconstruct` gettext tools | Existing `.po` entries and the string's runtime call site |
| Packaging and manifest generation | `sconstruct`, `site_scons`, `buildVars.py` | An existing package target and generated-file rules |
| Compatibility or deprecation questions | The developer guide and the target NVDA source version | Git history or release notes for the relevant API |

Paths in the table are examples: locate the exact module with `rg --files D:\git\nvda\source` when a name has changed between NVDA versions. Use supported public APIs when they cover the need. If no public API exists, prefer a small, well-understood reuse of the relevant NVDA private implementation over reimplementing equivalent behavior. Before doing so, read that implementation and its callers, identify the NVDA versions it depends on, and state the coupling and likely breakage point in the change. Keep the private usage narrow and isolated so it can be replaced when NVDA exposes or changes the behavior; add a compatibility guard when versions differ. If private reuse is unsuitable, implement the smallest local fallback and explain why.

## Coding and product rules

- User-facing strings go through `_()` (or the repository's established translation helper).
- Preserve accessibility behavior, focus handling, cancellation, error handling, and user data. Do not hide exceptions that matter to the user or logs.
- Do not change generated files, translations, manifests, package artifacts, or dependency versions unless the task requires them.
- Do not add English docstrings, comments, type abstractions, or configuration knobs solely to satisfy a generic rule; add them when they explain non-obvious behavior or are required by the local code.
- Keep user-visible behavior and documentation synchronized when the request changes them.

## Validation by change scope

Run checks based on what changed, and report what was skipped:

- Python code: `uv run ruff check <changed paths>`, `uv run ruff format --check <changed paths>`, and `python -m compileall <changed paths>`.
- Tests or behavior logic: run the smallest relevant `pytest` selection. Run the full suite only for broad changes or when requested.
- Type-heavy changes: run the repository's configured Pyright command when practical; do not turn unrelated existing type errors into scope.
- Packaging, `buildVars.py`, manifests, `sconstruct`, or build scripts: run `uv run scons`.
- User-facing strings or locale files: run the repository's gettext target, merge only affected locales, and run `msgfmt --check` for those files.
- Release changes: update release metadata and changelog only when requested; verify the generated package when packaging is part of the task.
- Every change: run `git diff --check` and `git status --short`.

Do not run a clean build, full translation regeneration, full test suite, or manual NVDA installation merely because a Python file changed. Never create commits, tags, releases, or install the add-on unless explicitly requested.

## Debugging and investigation

For a reported bug, reproduce or trace the complete path before editing: gesture/event entry, add-on handler, NVDA API call, and resulting speech/UI/state change. Search every caller of a function being changed. Prefer a focused log or test that distinguishes the suspected root cause from a symptom. If runtime behavior depends on NVDA version, state which version was inspected.

## Release and upstream workflows

Use the release skills only when the user explicitly requests that operation:

- `nvda-addon-version-bump` is for preparing or publishing a new version. It may commit, tag, push, wait for CI, and produce a store link; treat each as a separate requested action. A normal bug fix or package build does not invoke it.
- `nvda-addon-store-submit-link` is only for an already published GitHub release. It does not publish anything and should not be used to create a release.
- `nvda-addon-template-sync` is only for syncing from `nvaccess/AddonTemplate`. It is unrelated to ordinary feature work and does not justify local full tests or a clean build by default.

For release work, inspect repository state and release metadata first. Keep release notes in `buildVars.py` and `changelog.md` aligned. Build once after release files are ready; use `scons -c` only when stale generated output or a failed build gives a concrete reason. Do not repeatedly rebuild while editing unrelated files. Do not merge, push, tag, publish, install, or submit a store issue without explicit user intent for that step. When a skill's default workflow conflicts with the user's requested scope, follow the user's scope and record skipped steps.
## Communication

State assumptions, affected files, checks run, and checks intentionally skipped briefly. If a requested behavior conflicts with NVDA's API or supported versions, explain the conflict before choosing a workaround. Keep unrelated findings as follow-up suggestions.
