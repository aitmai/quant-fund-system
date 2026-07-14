"""
Flask app factory for the quant-fund-system GUI (DESIGN.md §11).

Deployed to Render per DESIGN.md's Phase 0 plan — see render.yaml at the
repo root. Locally: `python app.py` (repo root entrypoint) after
`.env` has DATABASE_URL set, same as every other script in this repo.
"""

from flask import Flask


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "quant-fund-gui-dev-key"  # fine for an internal, single-user tool

    from src.gui.routes import gui_bp
    app.register_blueprint(gui_bp)

    return app
