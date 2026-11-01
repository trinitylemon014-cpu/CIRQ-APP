from flask import Blueprint, render_template

bp = Blueprint("legal_pages", __name__)


@bp.get("/guidelines")
def guidelines():
    return render_template("legal/guidelines.html")


@bp.get("/privacy")
def privacy():
    return render_template("legal/privacy.html")
