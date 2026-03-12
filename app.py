import os
import pwd
import json
import pty
import fcntl
import termios
import struct
import mimetypes
import subprocess
import threading
import select
import tempfile
import signal
import time
import pam
import re
import hashlib
from datetime import datetime

from flask import (
    Flask,
    request,
    render_template,
    redirect,
    session,
    send_file,
    abort,
    url_for,
    flash,
    jsonify,
)
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "change-me-now")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

APP_DATA_FILE = os.path.join("data", "apps.json")
INSTALLED_APPS_FILE = os.path.join("data", "installed_apps.json")

p = pam.pam()
terminal_sessions = {}

RVIZ_SESSION = {
    "display": ":1",
    "vnc_port": 5901,
    "web_port": 6080,
    "xvfb": None,
    "x11vnc": None,
    "websockify": None,
    "rviz": None,
    "mode": None,
}

SIGNUP_REQUESTS_FILE = os.path.join("data", "signup_requests.json")


def load_catalog():
    if not os.path.exists(APP_DATA_FILE):
        return []
    with open(APP_DATA_FILE, "r") as f:
        return json.load(f)


def load_installed_apps():
    if not os.path.exists(INSTALLED_APPS_FILE):
        return []
    with open(INSTALLED_APPS_FILE, "r") as f:
        return json.load(f)


def save_installed_apps(data):
    os.makedirs("data", exist_ok=True)
    with open(INSTALLED_APPS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_home(user):
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return os.path.expanduser("~{}".format(user))


def safe_user_path(user, target_path):
    home = os.path.realpath(get_home(user))
    target = os.path.realpath(target_path)

    if target == home or target.startswith(home + os.sep):
        return target
    return home


def format_size(size):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return "{} {}".format(int(value), unit)
            return "{:.1f} {}".format(value, unit)
        value /= 1024.0
    return "{} B".format(size)


def build_breadcrumbs(current_path, home):
    rel = os.path.relpath(current_path, home)
    crumbs = [{"name": "Home", "path": home}]
    if rel == ".":
        return crumbs

    running = home
    for part in rel.split(os.sep):
        running = os.path.join(running, part)
        crumbs.append({"name": part, "path": running})
    return crumbs


def require_login():
    return "user" in session


def get_current_user():
    return session.get("user")


def is_installed(app_id):
    installed = load_installed_apps()
    return any(x["id"] == app_id for x in installed)


def install_app_from_catalog(app_item):
    app_id = app_item["id"]
    name = "miniapp_{}".format(app_id)
    port = app_item["port"]
    image = app_item["image"]
    command_type = app_item.get("command_type", "basic")
    volume = app_item.get("volume", "")

    if command_type == "filebrowser":
        cmd = [
            "docker", "run", "-d",
            "--restart=unless-stopped",
            "--name", name,
            "-p", "{}:80".format(port),
            "-v", "/home:/srv",
            image
        ]
        launch_port = port
    elif command_type == "portainer":
        cmd = [
            "docker", "run", "-d",
            "--restart=unless-stopped",
            "--name", name,
            "-p", "{}:9000".format(port),
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", "portainer_data:/data",
            image
        ]
        launch_port = port
    else:
        cmd = ["docker", "run", "-d", "--restart=unless-stopped", "--name", name]
        if volume:
            cmd.extend(["-v", volume])
        cmd.extend(["-p", "{0}:{0}".format(port), image])
        launch_port = port

    subprocess.run(cmd, check=True)

    installed = load_installed_apps()
    installed.append({
        "id": app_id,
        "name": app_item["name"],
        "icon": app_item["icon"],
        "port": launch_port,
        "url": "http://localhost:{}".format(launch_port)
    })
    save_installed_apps(installed)


def demote_to_user(username):
    pw = pwd.getpwnam(username)
    uid = pw.pw_uid
    gid = pw.pw_gid
    home = pw.pw_dir
    shell = pw.pw_shell or "/bin/bash"

    def result():
        os.setgid(gid)
        os.setuid(uid)
        os.environ["HOME"] = home
        os.environ["USER"] = username
        os.environ["LOGNAME"] = username
        os.environ["SHELL"] = shell
        os.chdir(home)

    return result


def set_pty_size(fd, rows, cols):
    size = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


def reader_thread(sid, fd):
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.1)
            if fd in r:
                data = os.read(fd, 4096)
                if not data:
                    break
                socketio.emit("terminal_output", {"data": data.decode(errors="ignore")}, room=sid)
    except Exception as e:
        socketio.emit("terminal_output", {"data": "\r\n[terminal closed: {}]\r\n".format(e)}, room=sid)
    finally:
        sess = terminal_sessions.pop(sid, None)
        if sess:
            try:
                os.close(sess["fd"])
            except Exception:
                pass


def run_code_for_user(user, language, code):
    home = get_home(user)
    env = os.environ.copy()
    env["HOME"] = home
    env["USER"] = user
    env["LOGNAME"] = user

    suffix_map = {
        "python": ".py",
        "javascript": ".js",
        "bash": ".sh",
    }
    suffix = suffix_map.get(language, ".txt")

    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, dir=home) as tmp:
        tmp.write(code)
        tmp_path = tmp.name

    try:
        if language == "python":
            cmd = ["python3", tmp_path]
        elif language == "javascript":
            cmd = ["node", tmp_path]
        elif language == "bash":
            cmd = ["bash", tmp_path]
        else:
            return {
                "ok": False,
                "stdout": "",
                "stderr": "Unsupported language: {}".format(language),
                "returncode": 1,
            }

        preexec = demote_to_user(user) if os.geteuid() == 0 else None

        result = subprocess.run(
            cmd,
            cwd=home,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=20,
            preexec_fn=preexec,
        )

        return {
            "ok": True,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "Execution timed out after 20 seconds.",
            "returncode": 124,
        }
    except FileNotFoundError as e:
        return {
            "ok": False,
            "stdout": "",
            "stderr": str(e),
            "returncode": 127,
        }
    except Exception as e:
        return {
            "ok": False,
            "stdout": "",
            "stderr": str(e),
            "returncode": 1,
        }
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def detect_ros_installation():
    ros_root = "/opt/ros"
    result = {
        "found": False,
        "ros_version": None,
        "distro": None,
        "rviz_command": None,
        "message": "",
    }

    if not os.path.isdir(ros_root):
        result["message"] = "ROS is not installed under /opt/ros."
        return result

    distros = sorted(
        [d for d in os.listdir(ros_root) if os.path.isdir(os.path.join(ros_root, d))]
    )

    if not distros:
        result["message"] = "No ROS distributions found in /opt/ros."
        return result

    ros2_distros = {
        "ardent", "bouncy", "crystal", "dashing", "eloquent",
        "foxy", "galactic", "humble", "iron", "jazzy", "rolling"
    }
    ros1_distros = {"kinetic", "lunar", "melodic", "noetic"}

    for distro in reversed(distros):
        if distro in ros2_distros:
            cmd = "bash -lc 'source /opt/ros/{}/setup.bash && command -v rviz2'".format(distro)
            proc = subprocess.run(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            if proc.returncode == 0 and proc.stdout.strip():
                result.update({
                    "found": True,
                    "ros_version": "2",
                    "distro": distro,
                    "rviz_command": "source /opt/ros/{}/setup.bash && ros2 run rviz2 rviz2".format(distro),
                    "message": "Detected ROS 2 ({}) with rviz2.".format(distro),
                })
                return result

    for distro in reversed(distros):
        if distro in ros1_distros:
            cmd = "bash -lc 'source /opt/ros/{}/setup.bash && command -v rviz'".format(distro)
            proc = subprocess.run(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            if proc.returncode == 0 and proc.stdout.strip():
                result.update({
                    "found": True,
                    "ros_version": "1",
                    "distro": distro,
                    "rviz_command": "source /opt/ros/{}/setup.bash && rviz".format(distro),
                    "message": "Detected ROS 1 ({}) with rviz.".format(distro),
                })
                return result

    result["message"] = "ROS was found, but rviz/rviz2 is not available in the detected distributions."
    return result


def get_display_env_for_gui():
    display = os.environ.get("DISPLAY", "").strip()
    wayland = os.environ.get("WAYLAND_DISPLAY", "").strip()

    if display:
        return {"ok": True, "display": display, "wayland": wayland, "message": "Using current DISPLAY."}

    if wayland:
        return {"ok": True, "display": "", "wayland": wayland, "message": "Using current WAYLAND display."}

    if os.path.exists("/tmp/.X11-unix/X0"):
        return {
            "ok": True,
            "display": ":0",
            "wayland": "",
            "message": "DISPLAY was not set, using fallback display :0."
        }

    return {
        "ok": False,
        "display": "",
        "wayland": "",
        "message": "No graphical display detected. RViz can only be launched on a host desktop session.",
    }


def launch_rviz_for_user(user):
    ros_info = detect_ros_installation()
    if not ros_info["found"]:
        return {"ok": False, "message": ros_info["message"]}

    gui_info = get_display_env_for_gui()
    if not gui_info["ok"]:
        return {"ok": False, "message": gui_info["message"]}

    home = get_home(user)
    env = os.environ.copy()
    env["HOME"] = home
    env["USER"] = user
    env["LOGNAME"] = user

    if gui_info["display"]:
        env["DISPLAY"] = gui_info["display"]
    if gui_info["wayland"]:
        env["WAYLAND_DISPLAY"] = gui_info["wayland"]

    xauth_path = os.path.join(home, ".Xauthority")
    if os.path.exists(xauth_path):
        env["XAUTHORITY"] = xauth_path

    preexec = demote_to_user(user) if os.geteuid() == 0 else None
    cmd = "bash -lc '{}'".format(ros_info["rviz_command"])

    try:
        subprocess.Popen(
            cmd,
            shell=True,
            cwd=home,
            env=env,
            preexec_fn=preexec,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {
            "ok": True,
            "message": "Launching RViz for ROS {} ({}).".format(ros_info["ros_version"], ros_info["distro"]),
            "ros_version": ros_info["ros_version"],
            "distro": ros_info["distro"],
        }
    except Exception as e:
        return {"ok": False, "message": "Failed to launch RViz: {}".format(e)}


def ros_rviz_command():
    ros_info = detect_ros_installation()
    if not ros_info["found"]:
        return None, ros_info["message"]

    if ros_info["ros_version"] == "2":
        return "bash -lc 'source /opt/ros/{}/setup.bash && ros2 run rviz2 rviz2'".format(ros_info["distro"]), None
    return "bash -lc 'source /opt/ros/{}/setup.bash && rviz'".format(ros_info["distro"]), None


def _is_running(proc):
    return proc is not None and proc.poll() is None


def _safe_killpg(proc):
    try:
        if proc is not None and proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass


def stop_rviz_browser_session():
    _safe_killpg(RVIZ_SESSION["rviz"])
    _safe_killpg(RVIZ_SESSION["websockify"])
    _safe_killpg(RVIZ_SESSION["x11vnc"])
    _safe_killpg(RVIZ_SESSION["xvfb"])

    RVIZ_SESSION["rviz"] = None
    RVIZ_SESSION["websockify"] = None
    RVIZ_SESSION["x11vnc"] = None
    RVIZ_SESSION["xvfb"] = None
    RVIZ_SESSION["mode"] = None


def start_rviz_browser_session():
    cmd, err = ros_rviz_command()
    if err:
        return {"ok": False, "message": err}

    novnc_web = "/usr/share/novnc"
    if not os.path.isdir(novnc_web):
        return {"ok": False, "message": "noVNC was not found at /usr/share/novnc. Install novnc first."}

    display = RVIZ_SESSION["display"]
    vnc_port = RVIZ_SESSION["vnc_port"]
    web_port = RVIZ_SESSION["web_port"]

    env = os.environ.copy()
    env["DISPLAY"] = display

    try:
        if not _is_running(RVIZ_SESSION["xvfb"]):
            RVIZ_SESSION["xvfb"] = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1600x900x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            time.sleep(1)

        if not _is_running(RVIZ_SESSION["x11vnc"]):
            RVIZ_SESSION["x11vnc"] = subprocess.Popen(
                ["x11vnc", "-display", display, "-forever", "-nopw", "-shared", "-rfbport", str(vnc_port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            time.sleep(1)

        if not _is_running(RVIZ_SESSION["websockify"]):
            RVIZ_SESSION["websockify"] = subprocess.Popen(
                ["websockify", "--web", novnc_web, str(web_port), "localhost:{}".format(vnc_port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            time.sleep(1)

        if not _is_running(RVIZ_SESSION["rviz"]):
            RVIZ_SESSION["rviz"] = subprocess.Popen(
                cmd,
                shell=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )

        RVIZ_SESSION["mode"] = "browser"

        host = request.host.split(":")[0]
        url = "http://{}:{}/vnc.html?host={}&port={}&autoconnect=true&resize=scale".format(
            host, web_port, host, web_port
        )

        return {
            "ok": True,
            "message": "RViz browser session started.",
            "url": url
        }
    except Exception as e:
        return {"ok": False, "message": "Failed to start RViz browser session: {}".format(e)}

def load_signup_requests():
    if not os.path.exists(SIGNUP_REQUESTS_FILE):
        return []
    with open(SIGNUP_REQUESTS_FILE, "r") as f:
        return json.load(f)


def save_signup_requests(data):
    os.makedirs("data", exist_ok=True)
    with open(SIGNUP_REQUESTS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def username_is_valid(username):
    """
    Linux-safe username rule:
    starts with lowercase letter or underscore,
    then lowercase letters, digits, underscore, dash allowed.
    """
    return re.match(r"^[a-z_][a-z0-9_-]{0,31}$", username) is not None


def linux_user_exists(username):
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def get_user_groups(username):
    try:
        output = subprocess.check_output(["id", "-nG", username], universal_newlines=True)
        return output.strip().split()
    except Exception:
        return []


def is_admin_user(username):
    groups = get_user_groups(username)
    return ("sudo" in groups) or (username == "root")


def hash_password_for_request(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def create_linux_user_account(username, password, full_name="", room_number="", work_phone="", home_phone="", other=""):
    """
    Create a real Linux user account.
    This requires root or sudo privileges.
    """
    gecos = "{},{},{},{}".format(
        full_name or username,
        room_number or "",
        work_phone or "",
        home_phone or ""
    )

    useradd_cmd = ["useradd", "-m", "-s", "/bin/bash", "-c", gecos, username]

    # If not root, use sudo
    if os.geteuid() != 0:
        useradd_cmd.insert(0, "sudo")

    subprocess.run(useradd_cmd, check=True)

    passwd_cmd = "echo '{}:{}' | {}".format(
        username,
        password,
        "chpasswd" if os.geteuid() == 0 else "sudo chpasswd"
    )
    subprocess.run(passwd_cmd, shell=True, check=True)

    if other:
        comment_cmd = "usermod -a -c '{}' {}".format(
            "{} | Other: {}".format(full_name or username, other).replace("'", ""),
            username
        )
        if os.geteuid() != 0:
            comment_cmd = "sudo " + comment_cmd
        subprocess.run(comment_cmd, shell=True, check=True)


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username and p.authenticate(username, password, service="login"):
            session["user"] = username
            return redirect(url_for("home"))

        return render_template("login.html", error="Invalid username or password.")

    return render_template("login.html", error=None)


@app.route("/home")
def home():
    if not require_login():
        return redirect(url_for("login"))
    user = get_current_user()
    installed = load_installed_apps()
    return render_template("home.html", user=user, installed=installed, is_admin=is_admin_user(user))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        retype_password = request.form.get("retype_password", "")
        full_name = request.form.get("full_name", "").strip()
        room_number = request.form.get("room_number", "").strip()
        work_phone = request.form.get("work_phone", "").strip()
        home_phone = request.form.get("home_phone", "").strip()
        other = request.form.get("other", "").strip()

        if not username or not password:
            return render_template(
                "signup.html",
                error="Username and password are mandatory."
            )

        if not username_is_valid(username):
            return render_template(
                "signup.html",
                error="Invalid username. Use lowercase letters, digits, underscore, and dash only."
            )

        if retype_password != password:
            return render_template(
                "signup.html",
                error="Retype password must match password."
            )

        if linux_user_exists(username):
            return render_template(
                "signup.html",
                error="That username already exists on this computer."
            )

        requests_data = load_signup_requests()

        if any(x["username"] == username and x["status"] == "pending" for x in requests_data):
            return render_template(
                "signup.html",
                error="A pending signup request already exists for this username."
            )

        # Full Name should be same as username
        full_name = username

        requests_data.append({
            "id": str(int(time.time() * 1000)),
            "username": username,
            "password_plain": password,
            "password_hash": hash_password_for_request(password),
            "full_name": full_name,
            "room_number": room_number,
            "work_phone": work_phone,
            "home_phone": home_phone,
            "other": other,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "reviewed_by": "",
            "reviewed_at": ""
        })

        save_signup_requests(requests_data)

        return render_template(
            "signup.html",
            success="Signup request submitted. A sudo user must approve it before the account is created."
        )

    return render_template("signup.html")


@app.route("/admin/signup-requests")
def admin_signup_requests():
    if not require_login():
        return redirect(url_for("login"))

    user = get_current_user()
    if not is_admin_user(user):
        abort(403)

    requests_data = load_signup_requests()
    return render_template(
        "admin_signup_requests.html",
        user=user,
        requests_data=requests_data
    )


@app.route("/admin/signup-requests/<request_id>/approve", methods=["POST"])
def approve_signup_request(request_id):
    if not require_login():
        return redirect(url_for("login"))

    admin_user = get_current_user()
    if not is_admin_user(admin_user):
        abort(403)

    requests_data = load_signup_requests()
    item = next((x for x in requests_data if x["id"] == request_id), None)

    if not item:
        flash("Request not found.")
        return redirect(url_for("admin_signup_requests"))

    if item["status"] != "pending":
        flash("Request already processed.")
        return redirect(url_for("admin_signup_requests"))

    if linux_user_exists(item["username"]):
        item["status"] = "rejected"
        item["reviewed_by"] = admin_user
        item["reviewed_at"] = datetime.utcnow().isoformat() + "Z"
        save_signup_requests(requests_data)
        flash("User already exists. Request rejected.")
        return redirect(url_for("admin_signup_requests"))

    try:
        create_linux_user_account(
            username=item["username"],
            password=item["password_plain"],
            full_name=item["full_name"],
            room_number=item["room_number"],
            work_phone=item["work_phone"],
            home_phone=item["home_phone"],
            other=item["other"]
        )
        item["status"] = "approved"
        item["reviewed_by"] = admin_user
        item["reviewed_at"] = datetime.utcnow().isoformat() + "Z"
        save_signup_requests(requests_data)
        flash("User account created successfully.")
    except Exception as e:
        flash("Approval failed: {}".format(e))

    return redirect(url_for("admin_signup_requests"))


@app.route("/admin/signup-requests/<request_id>/reject", methods=["POST"])
def reject_signup_request(request_id):
    if not require_login():
        return redirect(url_for("login"))

    admin_user = get_current_user()
    if not is_admin_user(admin_user):
        abort(403)

    requests_data = load_signup_requests()
    item = next((x for x in requests_data if x["id"] == request_id), None)

    if not item:
        flash("Request not found.")
        return redirect(url_for("admin_signup_requests"))

    if item["status"] != "pending":
        flash("Request already processed.")
        return redirect(url_for("admin_signup_requests"))

    item["status"] = "rejected"
    item["reviewed_by"] = admin_user
    item["reviewed_at"] = datetime.utcnow().isoformat() + "Z"
    save_signup_requests(requests_data)

    flash("Signup request rejected.")
    return redirect(url_for("admin_signup_requests"))

    
@app.route("/browser")
def browser():
    if not require_login():
        return redirect(url_for("login"))
    installed = load_installed_apps()
    return render_template("browser.html", user=get_current_user(), installed=installed)


@app.route("/app-store")
def app_store():
    if not require_login():
        return redirect(url_for("login"))

    catalog = load_catalog()
    installed = load_installed_apps()
    installed_ids = {x["id"] for x in installed}
    return render_template(
        "app_store.html",
        user=get_current_user(),
        apps=catalog,
        installed_ids=installed_ids
    )


@app.route("/install/<app_id>", methods=["POST"])
def install_app(app_id):
    if not require_login():
        return redirect(url_for("login"))

    catalog = load_catalog()
    app_item = next((x for x in catalog if x["id"] == app_id), None)
    if not app_item:
        flash("App not found.")
        return redirect(url_for("app_store"))

    if is_installed(app_id):
        flash("App already installed.")
        return redirect(url_for("app_store"))

    try:
        install_app_from_catalog(app_item)
        flash("{} installed successfully.".format(app_item["name"]))
    except Exception as e:
        flash("Install failed: {}".format(str(e)))

    return redirect(url_for("app_store"))


@app.route("/installed")
def installed():
    if not require_login():
        return redirect(url_for("login"))
    apps = load_installed_apps()
    return render_template("installed.html", user=get_current_user(), apps=apps)


@app.route("/files")
def files():
    if not require_login():
        return redirect(url_for("login"))

    user = get_current_user()
    home = os.path.realpath(get_home(user))
    requested_path = request.args.get("path", home)
    current_path = safe_user_path(user, requested_path)

    if not os.path.isdir(current_path):
        current_path = home

    entries = []
    try:
        names = os.listdir(current_path)
    except PermissionError:
        names = []

    for name in sorted(names, key=lambda x: x.lower()):
        full_path = os.path.join(current_path, name)
        is_dir = os.path.isdir(full_path)

        try:
            stat_info = os.stat(full_path)
            size = "-" if is_dir else format_size(stat_info.st_size)
        except Exception:
            size = "-"

        entries.append({
            "name": name,
            "path": full_path,
            "is_dir": is_dir,
            "size": size,
        })

    folders = [e for e in entries if e["is_dir"]]
    files_only = [e for e in entries if not e["is_dir"]]

    parent = os.path.dirname(current_path)
    if not parent.startswith(home):
        parent = home

    breadcrumbs = build_breadcrumbs(current_path, home)

    return render_template(
        "files.html",
        user=user,
        current_path=current_path,
        parent=parent,
        breadcrumbs=breadcrumbs,
        folders=folders,
        files=files_only,
        folder_count=len(folders),
        file_count=len(files_only),
    )


@app.route("/download")
def download():
    if not require_login():
        return redirect(url_for("login"))

    user = get_current_user()
    file_path = request.args.get("path", "")
    file_path = safe_user_path(user, file_path)

    if not os.path.isfile(file_path):
        abort(404)

    return send_file(file_path, as_attachment=True)


@app.route("/preview")
def preview():
    if not require_login():
        return redirect(url_for("login"))

    user = get_current_user()
    file_path = request.args.get("path", "")
    file_path = safe_user_path(user, file_path)

    if not os.path.isfile(file_path):
        abort(404)

    mime, _ = mimetypes.guess_type(file_path)
    if mime and (mime.startswith("image/") or mime.startswith("text/") or mime == "application/pdf"):
        return send_file(file_path)

    return redirect(url_for("download", path=file_path))


@app.route("/terminal")
def terminal():
    if not require_login():
        return redirect(url_for("login"))
    return render_template("terminal.html", user=get_current_user())


@app.route("/editor")
def editor():
    if not require_login():
        return redirect(url_for("login"))

    user = get_current_user()
    home = get_home(user)
    file_path = request.args.get("path", "").strip()

    content = ""
    current_path = ""

    if file_path:
        safe_path = safe_user_path(user, file_path)
        if os.path.isfile(safe_path):
            try:
                with open(safe_path, "r", encoding="utf-8") as f:
                    content = f.read()
                current_path = safe_path
            except Exception as e:
                flash("Could not open file: {}".format(e))

    return render_template(
        "editor.html",
        user=user,
        content=content,
        current_path=current_path,
        home=home,
    )


@app.route("/rviz")
def rviz_page():
    if not require_login():
        return redirect(url_for("login"))
    return render_template("rviz.html", user=get_current_user())


@app.route("/rviz-browser")
def rviz_browser_page():
    if not require_login():
        return redirect(url_for("login"))

    host = request.host.split(":")[0]
    novnc_url = "http://{}:{}/vnc.html?host={}&port={}&autoconnect=true&resize=scale".format(
        host,
        RVIZ_SESSION["web_port"],
        host,
        RVIZ_SESSION["web_port"]
    )
    return render_template("rviz_stream.html", user=get_current_user(), novnc_url=novnc_url)


@app.route("/api/read-file", methods=["POST"])
def api_read_file():
    if not require_login():
        return jsonify({"ok": False, "error": "Not authenticated"}), 401

    user = get_current_user()
    path = request.json.get("path", "")
    safe_path = safe_user_path(user, path)

    if not os.path.isfile(safe_path):
        return jsonify({"ok": False, "error": "File not found"}), 404

    try:
        with open(safe_path, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"ok": True, "content": content, "path": safe_path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/save-file", methods=["POST"])
def api_save_file():
    if not require_login():
        return jsonify({"ok": False, "error": "Not authenticated"}), 401

    user = get_current_user()
    path = request.json.get("path", "").strip()
    content = request.json.get("content", "")

    if not path:
        return jsonify({"ok": False, "error": "Path is required"}), 400

    safe_path = safe_user_path(user, path)

    try:
        parent = os.path.dirname(safe_path)
        os.makedirs(parent, exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"ok": True, "path": safe_path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/run-code", methods=["POST"])
def api_run_code():
    if not require_login():
        return jsonify({"ok": False, "error": "Not authenticated"}), 401

    user = get_current_user()
    language = request.json.get("language", "").strip().lower()
    code = request.json.get("content", "")

    result = run_code_for_user(user, language, code)
    return jsonify(result)


@app.route("/api/rviz-status")
def api_rviz_status():
    if not require_login():
        return jsonify({"ok": False, "message": "Not authenticated"}), 401

    ros_info = detect_ros_installation()
    gui_info = get_display_env_for_gui()

    return jsonify({
        "ok": True,
        "ros_found": ros_info["found"],
        "ros_version": ros_info["ros_version"],
        "distro": ros_info["distro"],
        "ros_message": ros_info["message"],
        "gui_ok": gui_info["ok"],
        "gui_message": gui_info.get("message", "GUI display available."),
        "display": gui_info.get("display", ""),
        "wayland": gui_info.get("wayland", ""),
        "browser_session_running": _is_running(RVIZ_SESSION["xvfb"]) and _is_running(RVIZ_SESSION["x11vnc"]) and _is_running(RVIZ_SESSION["websockify"])
    })


@app.route("/api/open-rviz", methods=["POST"])
def api_open_rviz():
    if not require_login():
        return jsonify({"ok": False, "message": "Not authenticated"}), 401

    result = launch_rviz_for_user(get_current_user())
    return jsonify(result)


@app.route("/api/open-rviz-browser", methods=["POST"])
def api_open_rviz_browser():
    if not require_login():
        return jsonify({"ok": False, "message": "Not authenticated"}), 401
    return jsonify(start_rviz_browser_session())


@app.route("/api/stop-rviz-browser", methods=["POST"])
def api_stop_rviz_browser():
    if not require_login():
        return jsonify({"ok": False, "message": "Not authenticated"}), 401

    stop_rviz_browser_session()
    return jsonify({"ok": True, "message": "RViz browser session stopped."})


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@socketio.on("terminal_connect")
def terminal_connect(data):
    if "user" not in session:
        emit("terminal_output", {"data": "\r\nNot authenticated.\r\n"})
        return

    user = session["user"]
    home = get_home(user)
    shell = os.environ.get("SHELL", "/bin/bash")

    master_fd, slave_fd = pty.openpty()
    rows = int(data.get("rows", 24))
    cols = int(data.get("cols", 80))
    set_pty_size(master_fd, rows, cols)

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["HOME"] = home
    env["USER"] = user
    env["LOGNAME"] = user
    env["SHELL"] = shell

    preexec = demote_to_user(user) if os.geteuid() == 0 else None

    proc = subprocess.Popen(
        [shell, "-l"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=home,
        env=env,
        preexec_fn=preexec,
        close_fds=True,
    )

    os.close(slave_fd)

    sid = request.sid
    terminal_sessions[sid] = {
        "fd": master_fd,
        "proc": proc,
        "user": user,
    }

    thread = threading.Thread(target=reader_thread, args=(sid, master_fd))
    thread.daemon = True
    thread.start()


@socketio.on("terminal_input")
def terminal_input(data):
    sid = request.sid
    sess = terminal_sessions.get(sid)
    if not sess:
        return

    text = data.get("data", "")
    if text:
        os.write(sess["fd"], text.encode())


@socketio.on("terminal_resize")
def terminal_resize(data):
    sid = request.sid
    sess = terminal_sessions.get(sid)
    if not sess:
        return

    rows = int(data.get("rows", 24))
    cols = int(data.get("cols", 80))
    set_pty_size(sess["fd"], rows, cols)


@socketio.on("disconnect")
def terminal_disconnect():
    sid = request.sid
    sess = terminal_sessions.pop(sid, None)
    if not sess:
        return

    try:
        sess["proc"].terminate()
    except Exception:
        pass

    try:
        os.close(sess["fd"])
    except Exception:
        pass


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)

    if not os.path.exists(INSTALLED_APPS_FILE):
        save_installed_apps([])

    if not os.path.exists(SIGNUP_REQUESTS_FILE):
        save_signup_requests([])

    socketio.run(app, host="0.0.0.0", port=8080, debug=True)