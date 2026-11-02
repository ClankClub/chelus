# Website

This is the GitHub Pages product site, separate from the Python agent runtime.

- Read `README.md` for build, deployment and the approved design.
- Use standard-library Python, shared templates, local CSS/JS and self-hosted assets. No runtime or pip dependencies.
- Keep setup aligned with the repository README; derive config defaults from source, not a copied table. Current verification belongs in `../docs/STATUS.md`.
- Preserve the approved editorial burgundy/cream design. Homepage imagery is illustrative, not evidence of a real installation.
- Verify `python -m unittest discover -s website/tests` from the repository root, a project-prefix build, and desktop/mobile browser navigation before publishing.
- Source: `build.py`, `content.py`, `templates/`, `assets/`. Output: ignored `dist/`. Deployment: `../.github/workflows/pages.yml`.
