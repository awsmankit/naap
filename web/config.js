// Build-time settings for the UI.
//
// mode: "server" calls the FastAPI app (uv run python -m app); "local" runs the engine in this
// browser (a Web Worker on Pyodide), as on the GitHub Pages site. scripts/build_site.py writes
// mode "local" into the site it builds. `?mode=local` or `?mode=server` in the URL overrides it.
export const CONFIG = { mode: "server" };
