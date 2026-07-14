"""
GUI entrypoint (DESIGN.md §11, Phase 8).

Local dev:   python app.py            (reads DATABASE_URL from .env)
Render:      gunicorn app:app          (see render.yaml)
"""

from dotenv import load_dotenv
load_dotenv()

from src.gui import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
