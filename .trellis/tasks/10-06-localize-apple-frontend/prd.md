# Localize and simplify frontend UI

## Goal

Present the R2 model scanner entirely in Simplified Chinese and make its login and dashboard feel like a restrained Apple system-management interface: clear typography, generous whitespace, quiet surfaces, fine separators, and limited blue emphasis.

## What I already know

- The React/Vite frontend has English labels, English API error strings, and English document metadata.
- The current visual layer uses prominent gradients, status pills, boxed statistics, and nested card styling that the user considers insufficiently simple.
- The user chose the Apple settings/management style over a promotional landing page.
- Keep all existing authentication, connection, scan progress, and report behavior. R2 interaction remains read-only.

## Requirements

- Translate every user-facing label, button, placeholder, loading state, status, and known API error into Simplified Chinese; preserve protocol identifiers such as `Access Key ID`, `Secret Access Key`, and the model path.
- Set the document language to `zh-CN` and use a Chinese browser title.
- Use a quiet, content-first management layout with ample spacing, restrained typography, subtle separators, minimal status decoration, and responsive layout.
- Do not change backend contracts, credentials handling, or R2 behavior.
- Preserve already-uncommitted proxy support and the React mount fix.

## Acceptance Criteria

- [ ] No English natural-language copy is visible anywhere in login, dashboard, errors, scan statuses, or reports.
- [ ] The result reads as a restrained, Chinese Apple-style settings interface rather than a generic admin template.
- [ ] Frontend lint and production build pass.
- [ ] No R2 scan or write operation is executed as part of this change.
- [ ] Append a progress record including verification, changed-file list, and rollback.

## Definition of Done

- Lint/typecheck and production frontend build pass.
- Relevant edited files are checked for linter diagnostics.
- Progress notes contain verification and rollback details.

## Out of Scope

- Backend/API behavior changes, real R2 connections or scans, synchronization, and new features.
- Replacing the application's React/Vite stack or adding dependencies.

## Technical Notes

- Frontend conventions: `.trellis/spec/frontend/index.md`, `components.md`, `css-layout.md`, `quality.md`.
- Existing public deployment uses `dataclean.lsynb.me`; production rollout requires rebuilding on Oracle without changing its volume or Nginx configuration.
- The current worktree already contains local changes in `Dockerfile`, `docs/deployment.md`, `frontend/src/main.tsx`, and `progress.md`; do not discard or overwrite unrelated changes.
{"file":".trellis/spec/frontend/index.md","reason":"Frontend pre-development conventions and the applicable guideline map."}
{"file":".trellis/spec/frontend/components.md","reason":"Semantic React markup and accessible component conventions."}
{"file":".trellis/spec/frontend/css-layout.md","reason":"Responsive CSS, touch target, and reduced-motion conventions."}
{"file":".trellis/spec/frontend/quality.md","reason":"Frontend lint, build, and quality expectations."}
{"file":".trellis/spec/frontend/index.md","reason":"Check the changes against frontend package conventions."}
{"file":".trellis/spec/frontend/css-layout.md","reason":"Verify responsive behavior, tap targets, and reduced motion."}
{"file":".trellis/spec/frontend/quality.md","reason":"Run the required frontend quality checks."}
