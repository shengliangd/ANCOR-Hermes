"""Standalone ANCOR Web viewer server (``ancor serve``).

Serves the read-only ANCOR dashboard API and static UI over any SQLite requirements.db,
with zero Hermes dependency. Designed so interactive mutations can be added later via AncorCore.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import AncorConfig, load_config
from .dashboard import plugin_api
from .visualizer import render_html


def create_serve_app(config: AncorConfig | None = None, *, store_path: str | Path | None = None) -> FastAPI:
    cfg = config or load_config(store_path_override=store_path)
    os.environ['ANCOR_STORE_PATH'] = str(cfg.store_path)
    os.environ['ANCOR_NOTIFICATIONS_PATH'] = str(cfg.notifications_path)

    app = FastAPI(title='ANCOR Dashboard', version='3.0.0a1')
    app.include_router(plugin_api.router, prefix='/api')
    # Also mount at root for clients calling /overview, /health, etc. directly.
    app.include_router(plugin_api.router)

    dist_dir = Path(__file__).resolve().parent / 'dashboard' / 'dist'

    @app.get('/', response_class=HTMLResponse)
    def index():
        if cfg.store_path.exists():
            return HTMLResponse(render_html(str(cfg.store_path)))
        return HTMLResponse(
            '<!doctype html><html><body><h2>ANCOR</h2>'
            f'<p>No store found yet at <code>{cfg.store_path}</code>.</p></body></html>'
        )

    if dist_dir.exists():
        app.mount('/assets', StaticFiles(directory=str(dist_dir)), name='ancor-assets')

    return app


def run_serve(
    *,
    host: str = '127.0.0.1',
    port: int = 8765,
    config_path: str | Path | None = None,
    store_path: str | Path | None = None,
) -> None:
    import uvicorn
    cfg = load_config(config_path, store_path_override=store_path)
    app = create_serve_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level='info')
