from functools import wraps
from flask import abort, current_app, redirect, request, session, url_for
from db import connect


def current_subscriber_id():
    value = session.get("subscriber_id")
    return int(value) if value is not None else None


def establish_subscriber_session(subscriber_id):
    """Authentication adapters call this only after verifying the subscriber."""
    session["subscriber_id"] = int(subscriber_id)
    session.permanent = True


def subscriber_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        subscriber_id = current_subscriber_id()
        if subscriber_id is not None:
            conn = connect(current_app.config["DB_PATH"])
            subscriber = conn.execute(
                "SELECT id,is_test FROM subscribers WHERE id=?", (subscriber_id,)
            ).fetchone()
            conn.close()
            if not subscriber or (subscriber["is_test"] and not current_app.config["ENABLE_TEST_IDENTITY"]):
                session.pop("subscriber_id", None)
                subscriber_id = None
        if subscriber_id is None:
            if current_app.config["ENABLE_TEST_IDENTITY"]:
                return redirect(url_for("test_identity", next=request.full_path))
            return redirect(url_for("magic_link_request", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped
