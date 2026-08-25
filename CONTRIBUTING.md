# Contributing

Thanks for your interest in improving `virtual-rtsp-camera`.

This project is maintained in limited spare time. Contributions are welcome, but we keep scope intentionally narrow to preserve maintainability.

## Before opening a pull request

1. For non-trivial changes, open an issue first and wait for maintainer feedback.
2. Keep changes focused and small.
3. Follow existing project structure and style.

## What is likely to be accepted

- Bug fixes with clear reproduction steps.
- Documentation improvements tied to current behavior.
- Tests that improve confidence without broad refactors.
- Small, scoped enhancements aligned with the roadmap.

## What is likely to be declined

- Large feature drops without prior discussion.
- Breaking API/CLI changes without strong justification.
- Broad refactors not tied to a user-facing issue.
- Changes that significantly increase maintenance cost.

## Development

```bash
uv sync
uv run pytest
```

If your change affects behavior, update docs and tests in the same pull request.

## Pull request checklist

- Link to the issue (or explain why none is needed).
- Include tests for behavior changes.
- Update documentation when user-facing behavior changes.
- Keep the PR narrowly scoped.

## Review and maintenance policy

- Reviews are best-effort and may take time.
- Maintainer may close or defer items outside the roadmap.
- Inactive PRs/issues may be marked stale and eventually closed.
