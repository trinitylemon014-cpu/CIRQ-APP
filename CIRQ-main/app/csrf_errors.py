from flask import flash, redirect, request, jsonify
from flask_wtf.csrf import CSRFError


def register_csrf_handler(app):
    @app.errorhandler(CSRFError)
    def csrf_failed(e):
        if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
            return jsonify(error="Your session expired. Refresh the page and try again."), 400
        flash("Your session expired. Please try again.", "error")
        return redirect(request.referrer or "/")
