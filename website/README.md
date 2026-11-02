# Chelus website

The product site and user documentation for Chelus. The design: warm cream, burgundy, large editorial serif headings, fine rules, illustrative phone photography, and a practical conversation demonstration. The homepage leads with useful agency on modest hardware; the Galaxy A10e supplies context rather than being the whole pitch.

## Build and preview

Python 3.11+ only. No package install, framework, pip dependencies, analytics, third-party font request, or client router.

```sh
python3 website/build.py --base-path /chelus/
python3 -m unittest discover -s website/tests
cd website
python3 dev.py --host 127.0.0.1 --port 4173
```

The preview redirects `/` to `/chelus/` and rebuilds when authored content, templates, or assets change. Open the `/chelus/` path on that local server. Generated output is ignored under `dist/` and `.preview/`.

## Routes and source

| Route | Role | Source |
| --- | --- | --- |
| `/` | Product story, demonstration, device context | `templates/home.html` |
| `/start/` | Requirements, install, key setup, first task | `content.py` |
| `/guide/` | Documentation introduction | `content.py` |
| `/guide/telegram/` | Bot setup and bridge limits | `content.py` |
| `/guide/memory-and-skills/` | Persistent facts and procedures | `content.py` |
| `/guide/notes/` | The notes folder | `content.py` |
| `/guide/tools-and-approvals/` | Tools, permissions, shell boundary | `content.py` |
| `/guide/scheduling/` | Scheduled prompts and script jobs | `content.py` |
| `/reference/` | Commands, defaults, paths, limitations | `content.py`, `../chelus/config.py` |

`build.py` assembles real HTML directory routes from the shared `templates/base.html`. `assets/site.css` holds the responsive design, and `assets/site.js` adds mobile menu behavior and clipboard buttons. Links, content, and navigation remain usable without JavaScript. All assets are self-hosted; the browser makes no external requests unless the visitor follows a link.

`{{base}}` in authored HTML is replaced with the configured deployment prefix. Default `/chelus/` matches the project Pages URL; `/` also works. Do not introduce browser-history routing or absolute `/assets/` links. Configuration defaults are extracted from the `Settings` AST without importing the agent or touching private runtime paths.

## GitHub Pages

`.github/workflows/pages.yml` checks pull requests and builds/deploys changes on `main`. Actions are pinned to verified release commit SHAs. Select **Settings → Pages → Build and deployment → Source: GitHub Actions**. Enabling Pages alone may leave a branch-source configuration; the workflow expects Actions mode.

The production URL is `https://clankclub.github.io/chelus/`. The configure step supplies the base path. For a future custom domain, update `ORIGIN` in `build.py` and the absolute social-image URL in `templates/base.html` as well as Pages settings.

Pull-request runs build and test only; they do not publish. Only `main` can deploy, with Pages write and OIDC permissions restricted to the deployment job. Never publish a new branch or change a Pages setting without the user's public-repository approval.

## Content and asset maintenance

- Setup and behavior must match `../README.md` and the implementation. Current live verification and release status belong in `../docs/STATUS.md`; link there rather than copying its changing test history.
- The site remains pre-1.0 until a release is actually made. Agent runtime is local; inference is remote via DeepSeek. Do not imply offline inference, universal device support, sandboxed shell access, or reliable Android background survival.
- `phone-front.webp` and `phone-back.webp` are AI-generated illustrative photography, visibly captioned. They are not evidence or photographs of the owner's installation. Replace them with real supplied device photography when available; keep the art direction and image dimensions. Generated imagery must not contain private real-world details.
- Brand symbol is reused verbatim from `../assets/chelus-header.svg` by the build. The social image is a raster rendering of that same existing header. No new logo was invented.
- Libre Caslon Display is self-hosted under its included OFL license. The system serif and monospace stacks are fallbacks and body faces. Bootstrap Icons SVGs are unmodified vendored upstream assets with their MIT license.

Before publishing: run the route and link tests, a production build and the repository tests, then check desktop and phone widths in a browser, including the menu, keyboard focus and copy buttons. Pushing to `main` publishes the site.
