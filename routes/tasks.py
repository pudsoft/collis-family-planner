"""Tasks blueprint — /tasks and all /tasks/* routes."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

import config
from modules import ntfy, tasks
from routes.utils import current_person, get_db, get_prefs

log = logging.getLogger(__name__)

bp = Blueprint("tasks", __name__)


@bp.route("/tasks")
def tasks_view():
    person = current_person()
    if "person" in request.args and request.args["person"] in config.PEOPLE + ["family"]:
        person = request.args["person"]
        session["person"] = person

    highlight_task = request.args.get("task", type=int)
    show_done      = request.args.get("show_done", "0") == "1"
    db             = get_db()
    prefs          = get_prefs(db, person)
    task_list      = tasks.get_tasks_for_person(db, person, include_done=show_done)

    return render_template(
        "tasks.html",
        person=person,
        prefs=prefs,
        task_list=task_list,
        highlight_task=highlight_task,
        show_done=show_done,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        today=date.today().isoformat(),
        is_admin=person in config.ADMINS,
    )


@bp.route("/tasks/create", methods=["POST"])
def task_create():
    d = request.form
    tasks.create_task(
        get_db(),
        title    = d.get("title", "").strip(),
        assignee = d.get("assignee", "anyone"),
        due_date = d.get("due_date") or None,
        notes    = d.get("notes") or None,
    )
    return redirect(url_for("tasks.tasks_view"))


@bp.route("/tasks/<int:task_id>/complete", methods=["POST"])
def task_complete(task_id: int):
    person = current_person()
    tasks.complete_task(get_db(), task_id, person)
    return jsonify({"ok": True})


@bp.route("/tasks/<int:task_id>/uncomplete", methods=["POST"])
def task_uncomplete(task_id: int):
    tasks.uncomplete_task(get_db(), task_id)
    return jsonify({"ok": True})


@bp.route("/tasks/<int:task_id>/defer", methods=["POST"])
def task_defer(task_id: int):
    defer_to = request.form.get("defer_to") or (date.today() + timedelta(days=1)).isoformat()
    reason   = request.form.get("reason")
    tasks.defer_task(get_db(), task_id, defer_to, reason)
    return jsonify({"ok": True})


@bp.route("/tasks/<int:task_id>/transfer", methods=["POST"])
def task_transfer(task_id: int):
    person    = current_person()
    if person not in config.ADMINS:
        return jsonify({"error": "Admins only"}), 403
    recipient = tasks.transfer_exec_function(get_db(), task_id, person)

    # Send NTFY to recipient
    db    = get_db()
    prefs = get_prefs(db, recipient)
    ch    = prefs.get("ntfy_channel")
    if ch:
        task_row = db.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task_row:
            ntfy.send_task_reminder(ch, recipient, task_id, task_row["title"], priority="high")

    return jsonify({"ok": True, "transferred_to": recipient})


@bp.route("/tasks/<int:task_id>/delete", methods=["POST"])
def task_delete(task_id: int):
    tasks.delete_task(get_db(), task_id)
    return jsonify({"ok": True})
