from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response, jsonify, render_template_string
from flask_login import login_required
from app.extensions import db
from app.models import MediaServer, Library, User
from app.forms.settings import SettingsForm  # reuse existing form for now
from app.services.servers import check_plex, check_jellyfin, check_emby, check_audiobookshelf, check_romm
from app.services.media.service import scan_libraries_for_server
import base64

media_servers_bp = Blueprint("media_servers", __name__, url_prefix="/settings/servers")


def _check_connection(data: dict):
    stype = data["server_type"]
    if stype == "plex":
        return check_plex(data["server_url"], data["api_key"])
    elif stype == "emby":
        return check_emby(data["server_url"], data["api_key"])
    elif stype == "audiobookshelf":
        return check_audiobookshelf(data["server_url"], data["api_key"])
    elif stype == "romm":
        username = data.get("server_username", "").strip()
        password = data.get("server_password", "").strip()
        if username and password:
            data["api_key"] = base64.b64encode(f"{username}:{password}".encode()).decode()
        return check_romm(data["server_url"], data["api_key"])
    else:
        return check_jellyfin(data["server_url"], data["api_key"])


@media_servers_bp.route("", methods=["GET"])  # list all
@login_required
def list_servers():
    servers = MediaServer.query.order_by(MediaServer.name).all()
    if request.headers.get('HX-Request'):
        return render_template('settings/servers.html', servers=servers)
    return render_template('settings/servers.html', servers=servers)


@media_servers_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_server():
    if request.method == "POST":
        data = request.form.to_dict()

        # --- RomM: derive API key from credentials ------------------
        if data.get("server_type") == "romm":
            username = data.get("server_username", "").strip()
            password = data.get("server_password", "").strip()
            if username and password:
                data["api_key"] = base64.b64encode(f"{username}:{password}".encode()).decode()

        ok, error_msg = _check_connection(data)
        if not ok:
            # Re-render modal with error
            resp = make_response(render_template("modals/create-server.html", error=error_msg))
            resp.headers["HX-Retarget"] = "#create-server-modal"
            return resp
        server = MediaServer(
            name=data["server_name"],
            server_type=data["server_type"],
            url=data["server_url"],
            api_key=data.get("api_key"),
            external_url=data.get("external_url"),
            allow_downloads_plex=bool(data.get("allow_downloads_plex")),
            allow_tv_plex=bool(data.get("allow_tv_plex")),
            
            # Jellyfin defaults
            allow_downloads_jellyfin=bool(data.get("allow_downloads_jellyfin")),
            allow_tv_jellyfin=bool(data.get("allow_tv_jellyfin")),
            verified=True,
        )
        db.session.add(server)
        db.session.commit()
        # attach chosen libraries
        chosen = request.form.getlist('libraries')
        if chosen:
            for fid in chosen:
                lib = Library.query.filter_by(external_id=fid, server_id=server.id).first()
                if lib:
                    lib.enabled = True
                else:
                    db.session.add(Library(external_id=fid, name=fid, server_id=server.id, enabled=True))
            db.session.commit()
        return redirect(url_for("media_servers.list_servers"))
    # GET
    return render_template("modals/create-server.html", error="")


@media_servers_bp.post('/<int:server_id>/scan-libraries')
@login_required
def scan_server_libraries(server_id):
    server = MediaServer.query.get_or_404(server_id)
    try:
        items = scan_libraries_for_server(server)
    except Exception as exc:
        flash(f"Library scan failed: {exc}", "error")
        return "<div class='text-red-500'>Failed</div>", 500

    # items may be dict or list[str]
    pairs = items.items() if isinstance(items, dict) else [(name, name) for name in items]

    seen_ids = set()
    for fid, name in pairs:
        seen_ids.add(fid)
        lib = Library.query.filter_by(external_id=fid, server_id=server.id).first()
        if lib:
            lib.name = name
        else:
            db.session.add(Library(external_id=fid, name=name, server_id=server.id))
    db.session.commit()

    # Render checkboxes partial (reuse existing partials)
    all_libs = Library.query.filter_by(server_id=server.id).order_by(Library.name).all()
    return render_template('partials/library_checkboxes.html', libs=all_libs)


@media_servers_bp.route("/<int:server_id>/edit", methods=["GET", "POST"])
@login_required
def edit_server(server_id):
    server = MediaServer.query.get_or_404(server_id)
    if request.method == "POST":
        data = request.form.to_dict()

        if data.get("server_type") == "romm":
            username = data.get("server_username", "").strip()
            password = data.get("server_password", "").strip()
            if username and password:
                data["api_key"] = base64.b64encode(f"{username}:{password}".encode()).decode()

        ok, error_msg = _check_connection(data)
        if not ok:
            resp = make_response(render_template("modals/edit-server.html", server=server, error=error_msg))
            resp.headers["HX-Retarget"] = "#create-server-modal"
            return resp
        server.name = data["server_name"]
        server.server_type = data["server_type"]
        server.url = data["server_url"]
        server.api_key = data.get("api_key")
        server.external_url = data.get("external_url")
        server.allow_downloads_plex = bool(data.get("allow_downloads_plex"))
        server.allow_tv_plex = bool(data.get("allow_tv_plex"))
        server.allow_downloads_jellyfin = bool(data.get("allow_downloads_jellyfin"))
        server.allow_tv_jellyfin = bool(data.get("allow_tv_jellyfin"))
        # update libraries
        chosen = request.form.getlist('libraries')
        if chosen:
            for lib in Library.query.filter_by(server_id=server.id):
                lib.enabled = lib.external_id in chosen
        db.session.commit()
        return redirect(url_for("media_servers.list_servers"))
    # GET → modal
    return render_template("modals/edit-server.html", server=server, error="")


@media_servers_bp.route("/", methods=["DELETE"])
@login_required
def delete_server():
    server_id = request.args.get("delete")
    if server_id:
        # 1) Delete local DB users that belong to this server so no ghost
        #    accounts linger once the server entry is gone.
        (
            User.query
            .filter(User.server_id == int(server_id))
            .delete(synchronize_session=False)
        )

        # 2) Finally remove the MediaServer itself.
        MediaServer.query.filter_by(id=server_id).delete(synchronize_session=False)
        db.session.commit()
    return "", 204


# -------------------------- NEW ENDPOINT ---------------------------
# Quick connectivity check for a given server. Returns {connected: bool, error: str}
# This is used by the settings UI to display real-time connection status on page load.


@media_servers_bp.get("/<int:server_id>/ping")
@login_required
def ping_server(server_id):
    """Return JSON reflecting whether a media server is currently reachable."""
    server = MediaServer.query.get_or_404(server_id)

    ok, error_msg = _check_connection(
        {
            "server_type": server.server_type,
            "server_url": server.url,
            "api_key": server.api_key,
        }
    )

    # If HTMX request, return a badge HTML snippet so it can replace itself.
    if request.headers.get("HX-Request"):
        if ok:
            cls = "bg-green-100 text-green-800 dark:bg-green-700 dark:text-green-100"
            text = "Connected"
        else:
            cls = "bg-red-100 text-red-800 dark:bg-red-700 dark:text-red-100"
            text = "Connection Error"

        snippet = render_template_string(
            '<span class="inline-flex rounded-full px-2 py-0.5 text-xs font-medium ' + cls + '" title="{{ error_msg }}">{{ text }}</span>',
            text=text,
            error_msg=error_msg or ""
        )
        return snippet

    # Non-HTMX fallback (e.g., API call)
    return jsonify({"connected": ok, "error": error_msg if not ok else None}) 