from functools import wraps
from flask import abort, current_app, redirect, request, session, url_for


def current_subscriber_id():
    value = session.get("subscriber_id")
    return int(value) if value is not None else None


def subscriber_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_subscriber_id() is None:
            if current_app.config["ENABLE_TEST_IDENTITY"]:
                return redirect(url_for("test_identity", next=request.full_path))
            abort(401)
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

