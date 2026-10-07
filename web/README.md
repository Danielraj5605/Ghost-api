# Ghost API dashboard and report (frontend)

The web dashboard (`ghost-api ui`) and the shareable `report.html` viewer. React 19, Vite,
TypeScript (strict) and Tailwind CSS v4.

You only need this folder to **change** the frontend. The built files are committed into the
Python package, so `pip install` users never need Node.js:

| Build | Source entry | Output (committed) |
|---|---|---|
| Dashboard | `index.html` → `src/main.tsx` | `ghost_api/web/static/` |
| Report viewer | `report.html` → `src/report/main.tsx` | `ghost_api/export/viewer/report.html` (one file, everything inlined) |

## Commands

Needs Node.js 20+.

```bash
npm install
npm run build       # type check, then build both outputs into the Python package
npm run typecheck
npm run lint
npm test            # Vitest
```

Commit the rebuilt files in `ghost_api/web/static/` and `ghost_api/export/viewer/` with
your source changes.

## Hot reload

The server needs its per-launch token on every request. For development, choose a fixed one
and give it to both sides.

Command Prompt:

```bat
set GHOST_API_UI_TOKEN=dev-token
ghost-api ui --no-open
:: in a second window, in web\
set VITE_GHOST_TOKEN=dev-token
npm run dev
```

PowerShell:

```powershell
$env:GHOST_API_UI_TOKEN = "dev-token"; ghost-api ui --no-open
# in a second window, in web\
$env:VITE_GHOST_TOKEN = "dev-token"; npm run dev
```

macOS / Linux:

```bash
GHOST_API_UI_TOKEN=dev-token ghost-api ui --no-open
# in a second terminal, in web/
VITE_GHOST_TOKEN=dev-token npm run dev
```

Then open the address Vite prints (usually `http://localhost:5173`). It forwards `/api` to
the server on port 8800.

## Layout

```
src/
|-- types.ts              # Shapes of the server's JSON (mirror ghost_api/events.py)
|-- api.ts                # Fetch wrapper that adds the token
|-- state/runReducer.ts   # Folds a run's events into what the live page shows
|-- hooks/useRunStream.ts # Server-Sent Events for a run (stored events, then live ones)
|-- components/           # Badges, timeline, live browser, endpoints, schema tree, code...
|-- pages/                # Home, Run, Site, Settings
+-- report/               # The report.html viewer (reuses the components)
```

Colors are theme tokens in `src/index.css` (dark and light), matching the terminal UI.
