# DAO Transformer — Serverless Browser Edition

This project is the correct architecture for a site that can be hosted as static files:
GitHub Pages, Cloudflare Pages, Netlify, etc. No Python server is required.

## Important status

The supplied `patcher.py` is a Python byte-level MP4 transformer. A browser cannot
run that Python file directly. The `src/transformer.js` file therefore starts a
real JavaScript port of the reference rather than pretending that the Python code
works in a browser.

The current build includes:
- polished static UI
- local MP4 selection/drag-and-drop
- browser-side MP4 box parsing
- explicit rejection of unsupported structures
- no upload endpoint
- no server dependency

The byte-level DAO rewrite is intentionally not faked yet. The reference needs its
sample-table/track/offset logic ported and verified against the user's MP4 test set
before the site can safely emit transformed files.

## Host

Upload the project files to any static host. `index.html`, `src/`, and `public/` are
all that are needed for the UI/JS.

No Flask, Python runtime, database, or server API is required for the finished
browser-only architecture.
