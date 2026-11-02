"""Gunicorn settings. Render provides $PORT; uploads can be large so the timeout is generous."""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", 2))
threads = 4
timeout = 180
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = "*"      # Render terminates TLS in front of gunicorn
