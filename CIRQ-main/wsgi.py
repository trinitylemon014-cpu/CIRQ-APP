"""WSGI entry point. Render/Gunicorn start command: gunicorn wsgi:app

(Named wsgi.py because a top-level app.py would be shadowed by the `app/` package.)"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
