#!/usr/bin/python3

import argparse
import base64
from enum import Enum
from http import HTTPStatus
import json
import logging
from logging.config import dictConfig
import mimetypes
import os
import re
import stat
import threading
import types
from functools import wraps

import gevent
from gevent.pywsgi import WSGIServer

from django.utils import text
from flask import Flask, request, Response, render_template_string, \
    send_file, redirect, url_for

try:
    from pyftpdlib.authorizers import AuthenticationFailed
    from pyftpdlib.filesystems import AbstractedFS, FilesystemError
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

except ModuleNotFoundError:
    AbstractedFS = object

from configparser import ConfigParser, ExtendedInterpolation, NoOptionError


class Access(Enum):
    LIST = "list"
    FETCH = "read"
    DELETE = "delete"
    UPLOAD = "write"
    MKDIR = "mkdir"


logging.basicConfig()

UNAUTH = "anonymous"

TEMPLATE = """<!doctype html>
<html lang="de">
  <head>
    <meta charset="utf-8">
    <title>File Upload Server for {{user}}</title>
    <style>
      #head {
      overflow: hidden:
      position: relative;
      display: grid;
      grid-template-columns: 1fr auto;
      width: 100%;
      }
      #msgbox {
      vertical-align: top;
      float: left;
      border: 1px solid black;
      }
      #loginbox {
      vertical-align: top;
      float: right;
      border: 1px solid black;
      }
      a {
      color: blue;
      }
      #dir {
      width:100%;
      float: left;
      }
      #row {
      display: block;
      width: 95%;
      margin-left: 2%;
      float: left;
      }
      #link {
      width: 40%;
      float: left;
      }
      #full_link {
      width: 40%;
      float: left;
      }
      #delete {
      width: 5%;
      float: right;
      }
      #content {
      }
      .fullhr {
      width: 100%;
      float: left;
      }
      .linkbutton {
      background:none!important;
      color:blue;
      border:none;
      padding:0!important;
      font: inherit;
      border-bottom:1px solid #444;
      cursor: pointer;
      }
    </style>
  </head>
  <div id="head">
    <div id="msgbox">
      {% if status -%}
      <div id="status">
        <label>Status message: </label>
        <p>
        {{ status }}
      </div>
      {% else -%}
      <div id="privacy">
        <a href="/privacy/{{dirname}}">privacy</a>
      </div>
      {% endif -%}
    </div>
    <div id="loginbox">
      <form method="POST" action="/">
        <table>
          <tr>
            <td>
              <label>User</label>
            </td>
            <td>
              <input type="text" name="user"  value="{{user}}" maxlength="30">
            </td>
          </tr>
          <tr>
            <td>
              <label>Password</label>
            </td>
            <td>
              <input type="password" name="password" maxlength="30">
            </td>
          </tr>
          <tr>
            <td colspan="2">
              <button type="submit">sign in</button>
            </td>
          </tr>
        </table>
      </form>
    </div>
  </div>
  <div id="content">
  {% if allow_upload -%}
  <hr class="fullhr">
  <form action="/{{dirname}}" method="post" enctype="multipart/form-data">
    <input type="file" name="file" />
    <input type="hidden" name="action" value="upload" />
    <button type="submit">upload</button>
  </form>
  {% endif -%}
  {% if files -%}
  <hr class="fullhr">
  {% for f in files -%}
  <p>
  <div id="row">
  </div>
  {% if allow_fetch -%}
  <div id="link">
    <a href="{{prefix}}{{f["path"]}}">
      {{f["name"]}}
    </a>
  </div>
  <div id="full_link">{{prefix}}{{f["path"]}}</div>
  {% else -%}
  {{f["name"]}}
  {% endif -%}
  {% if allow_delete -%}
  <form action="/{{f["path"]}}" method="post">
    <input type="hidden" name="action" value="delete" />
    <button type="submit">delete</button>
  </form>
  {% endif -%}
  {% endfor -%}
  {% endif -%}
  {% if subdirs or parentdir != None or allow_mkdir -%}
  <hr class="fullhr">
  {% if parentdir != None -%}
  <div id="row">
  <div id="link">
    <a href="{{prefix}}{{parentdir}}">
      &lt;parent directory&gt;
    </a>
  </div>
  {% endif -%}
  {% if allow_mkdir -%}
  <div id="row">
  <form action="/{{dirname}}" method="POST">
    <input type="hidden" name="action" value="mkdir" />
    <input type="text" name="dirname" />
    <button type="submit">new directory</button>
  </form>
  </div>
  {% endif -%}
  {% for s in subdirs -%}
  <div id="row">
  <a href="{{prefix}}{{s["path"]}}">
    {{s["name"]}}
  </a>
  </form>
  </div>
  {% endfor -%}
  <hr class="fullhr">
  {% endif -%}
  </div>
</html>
"""


class AccessError(Exception):

    def __init__(self, code, message):
        super().__init__(self, message, code)


letsencrypt_data = dict()


def get_all_user(config, section, access):
    result = set()
    g_access = "%s_groups" % access.value
    u_access = "%s_user" % access.value
    if config.has_option(section, g_access):
        groups = config.getlist(section, g_access)
    else:
        groups = []
    group_sections = ["group:" + s for s in groups]
    for gs in group_sections:
        if not config.has_section(gs):
            logging.error("No section '%s'", gs)
            continue
        result.update(set(config.getlist(gs, "user")))
    if config.has_option(section, u_access):
        result.update(set(config.getlist(section, u_access)))
    return result


def init_actions(perms, user):
    u = perms.setdefault(user, dict())
    for i in Access:
        u[i] = []


def compute_permissions(config):

    def mk_creds(config, section):
        if config.has_option(section, "password"):
            return config.get(section, "password")
        else:
            bytebuf = base64.b64decode(config.get(section, "b64_password"))
            return bytebuf.decode("utf8")

    user_perms = {UNAUTH: {"creds": None}}
    init_actions(user_perms, UNAUTH)

    for section in config.sections():
        if section.startswith("user:"):
            user = section[5:]
            user_perms[user] = {"creds": mk_creds(config, section)}
            init_actions(user_perms, user)

    all_dirs = set()
    for section in config.sections():
        if section.startswith("dir:"):
            dir_name = section[4:]
            all_dirs.add(dir_name)
            for access in Access:
                all_user = get_all_user(config, section, access)
                for user in all_user:
                    if user not in user_perms:
                        logging.error("User '%s' unconfigured", user)
                        continue
                    user_perms[user][access].append(dir_name)
    config.user_perms = user_perms
    config.all_dirs = list(all_dirs)
    config.all_dirs.sort()


def load_config():

    def get_list(conf, section, option, **kwargs):
        value = conf.get(section, option, **kwargs).strip()
        return [x.strip() for x in value.split(",")] if value else []

    parser = argparse.ArgumentParser(prog='file upload server')
    parser.add_argument("--config",
                        help="configuration file location",
                        dest="config",
                        default="/etc/fus.conf")

    args = parser.parse_args()

    config = ConfigParser(interpolation=ExtendedInterpolation())
    config.getlist = types.MethodType(get_list, config)
    config.read(args.config)
    # normalize basedir
    config.set("global", "basedir",
               os.path.abspath(config.get("global", "basedir")))

    compute_permissions(config)
    return config


def setup_logging():
    log_config = json.loads(config.get("logging", "config"))
    dictConfig(log_config)


def setup_app():
    # create data dirs, if needed
    basedir = config.get("global", "basedir")
    for section in config.sections():
        if section.startswith("dir:"):
            dir_name = os.path.join(basedir, section[4:])
            if not os.access(dir_name, os.R_OK | os.X_OK | os.W_OK):
                os.makedirs(dir_name)

    app = Flask(__name__)
    app.config['DEBUG'] = config.getboolean("global", "debug")
    return app


def run_server(app):
    server = []
    if config.has_option("global", "https_port"):
        https_server = WSGIServer((config.get("global", "host"),
                                   config.getint("global", "https_port")),
                                  app,
                                  keyfile=config.get("global", "keyfile"),
                                  certfile=config.get("global", "certfile"))
        https_server.start()
        server.append(https_server)

    if config.has_option("global", "http_port"):
        http_server = WSGIServer((config.get("global", "host"),
                                  config.getint("global", "http_port")),
                                 app)
        http_server.start()
        server.append(http_server)

    return server


config = load_config()

setup_logging()

app = setup_app()


@app.after_request
def after_request(response):
    response.headers.remove('Accept-Ranges')
    response.headers.add('Accept-Ranges', 'bytes')
    return response


def get_user_from_request(path):
    try:
        # form keys first
        user = request.values.get("user", None)
        password = request.values.get("password", None)

        if user is not None and password is not None:
            return user, password

        # basic-auth second
        auth = request.authorization
        if auth:
            return auth["username"], auth["password"]

        # credentials from cred cookie
        cred = request.cookies.get("cred", None)
        if cred:
            return base64.b64decode(cred).decode("utf8").split(":", 1)

    except Exception:
        logging.info("Error", exc_info=True)
    return UNAUTH, None


def get_user(path=None):
    """Get username either from cookie, basic auth header, or form keys"""

    user, password = get_user_from_request(path)

    if config.user_perms.get(user, dict()).get("creds", None) == password:
        logging.info("%s - - User %s active", request.remote_addr, user)
        cred = ("%s:%s" % (user, password)).encode("ascii")
        cred = base64.b64encode(cred).decode("ascii")
        return user, password, cred

    logging.warning("%s - - Anonymous active %s:%s",
                    request.remote_addr, user, password)
    return UNAUTH, None, ""


def redirect_on_exception(func):
    @wraps(func)
    def __wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AccessError as e:
            code = e.args[2]
            logging.getLogger(__name__).error("AccessError in function")
            print(code)
            if code == 401:
                return Response('Unauthorized',
                                code,
                                {'WWW-Authenticate': 'Basic realm="fus login"'}
                                )
            else:
                return Response(e.args[1], code, mimetype="text/plain")
        except Exception:
            logging.getLogger(__name__).error("Error in function",
                                              exc_info=True)
            return redirect("/", code=302)
    return __wrapper


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    favicon = config.get("global", "favicon")
    return Response(base64.b64decode(favicon), mimetype="image/gif")


@app.route("/.well-known/acme-challenge/<token>", methods=["GET"])
def serve_letsencrypt(token):
    logging.getLogger(__name__).info("%s - - cert verification request %s",
                                     request.remote_addr, token)
    if token in letsencrypt_data:
        return Response(letsencrypt_data[token], 200, mimetype="text/plain")
    else:
        return Response("permission denied", 403, mimetype="text/plain")


@app.route("/.well-known/acme-challenge/upload/<token>/<thumb>",
           methods=["GET"])
def upload_letsencrypt(token, thumb):
    logging.getLogger(__name__).info("%s - - cer verification file %s.%s",
                                     request.remote_addr, token, thumb)
    global letsencrypt_data
    letsencrypt_data[token] = "%s.%s" % (token, thumb)
    return Response("ok", 200, mimetype="text/plain")


def upload_file(user, directory):
    file = request.files['file']
    fname = text.get_valid_filename(os.path.basename(file.filename))
    name = os.path.join(directory, fname)
    fullname = os.path.join(config.get("global", "basedir"), name)
    if not fname:
        return redirect(url_for("handle", path=directory,
                                status="invalid filename"),
                        code=302)
    if os.access(fullname, os.R_OK):
        return Response("duplicate", 403)
    file.save(fullname)
    logging.info("%s - - File %s uploaded by %s",
                 request.remote_addr, name, user)
    size = os.lstat(fullname).st_size
    return redirect(url_for("handle", path=directory,
                            status="%d bytes saved as %s" % (size, fname)),
                    code=302)


def delete_file(user, dirname, filename):
    name = os.path.join(dirname, filename)
    fullname = os.path.join(config.get("global", "basedir"), name)
    try:
        os.remove(fullname)
        logging.getLogger(__name__).info("%s - - File %s deleted by %s",
                                         request.remote_addr,
                                         fullname,
                                         user)
        return redirect(url_for("handle", path=dirname,
                                status="File %s deleted" % name),
                        code=302)
    except Exception:
        logging.error("%s - - Failed to delete %s by %s",
                      request.remote_addr,
                      fullname,
                      user)
    return Response("permission denied", 403)


def make_dir(user, dirname):
    filename = text.get_valid_filename(request.values["dirname"])
    name = os.path.join(dirname, filename)
    fullname = os.path.join(config.get("global", "basedir"), name)
    try:
        os.makedirs(fullname)
        logging.getLogger(__name__).info("%s - - Directory %s created by %s",
                                         request.remote_addr,
                                         fullname,
                                         user)
        return redirect(url_for("handle", path=dirname,
                                status="Directory %s created" % name),
                        code=302)
    except Exception:
        logging.error("%s - - Failed to create directory %s by %s",
                      request.remote_addr,
                      fullname,
                      user)
    return Response("permission denied", 403)


def get_mime_type(f):
    type = mimetypes.guess_type(f, strict=False)[0]
    return type if type is not None else "application/octet-stream"


def normalize_path(path):
    basedir = config.get("global", "basedir")
    name = os.path.abspath(os.path.join(basedir, path))
    path = name[len(basedir):].strip(os.path.sep)

    if name != basedir and not name.startswith(basedir + os.path.sep):
        logging.warn("Outside base: %s", name)
        raise AccessError(403, "forbidden")
    try:

        sr = os.stat(name)
    except FileNotFoundError:
        logging.warn("File not found: %s", name)
        raise AccessError(403, "forbidden")
    except Exception:
        logging.error("Stat error on %s", name)
        raise AccessError(403, "forbidden")

    if sr.st_uid != os.geteuid():
        logging.warn("Wrong owner found for %s", name)

    if stat.S_ISDIR(sr.st_mode):
        return name, path, None

    if stat.S_ISREG(sr.st_mode):
        dirname, fname = os.path.split(path)
        return name, dirname, fname

    raise AccessError(403, "forbidden")


def has_access(user, dirname, action):
    perms = config.user_perms.get(user, None)
    if perms is None:
        logging.error("Unknown user: %s", user)
        raise AccessError(403, "forbidden")

    dirs = perms.get(action, None)
    if dirs is None:
        logging.error("Unknown action: %s", action)
        raise AccessError(403, "forbidden")

    while True:
        if dirname in config.all_dirs:
            return dirname in dirs
        if len(dirname) == 0 or dirname == os.path.sep:
            return False
        dirname, _ = os.path.split(dirname)


def list_dir(dirname):
    names = os.listdir(dirname)
    dirs = []
    files = []
    for n in names:
        try:
            if n.startswith("."):
                continue
            sr = os.stat(os.path.join(dirname, n))
            if sr.st_uid != os.geteuid():
                continue
            if stat.S_ISDIR(sr.st_mode):
                dirs.append(n)
            if stat.S_ISREG(sr.st_mode):
                files.append(n)
        except Exception:
            logging.exception("Error in list_dir(%s)", dirname)
    return dirs, files


def filter_file_list(user, dirname, subdirs, files):
    files.sort()
    subdirs.sort()
    if not has_access(user, dirname, Access.LIST):
        files.clear()

    for d in subdirs[:]:
        path = os.path.join(dirname, d)
        if not (has_access(user, path, Access.LIST)
                or has_access(user, path, Access.UPLOAD)
                or has_access(user, path, Access.MKDIR)):
            subdirs.remove(d)


def mk_prefix():
    return "http%s://%s/" % ("s" if request.is_secure else "", request.host)


@app.route("/privacy", defaults={'path': ''}, methods=["GET", "POST"])
@app.route("/privacy/", defaults={'path': ''}, methods=["GET", "POST"])
@app.route("/privacy/<path:path>", methods=["GET", "POST"])
def privacy(path):
    user, password, cred = get_user()
    return redirect(url_for("handle", path=path,
                            status=config.get("global", "gdprmsg")),
                    code=302)


def streamfile(fullname):

    def send_chunks_iter(filename, start, length):
        with open(filename, 'rb') as f:
            offset = 0
            while offset < length:
                f.seek(start + offset)
                chunk = config.getint("global", "chunksize")
                if offset + chunk > length:
                    chunk = length - offset
                offset += chunk
                logging.getLogger(__name__).debug("Sending %d-%d of %s",
                                                  start+offset,
                                                  start+offset+length,
                                                  filename)
                yield f.read(chunk)

    range_header = request.headers.get('Range', None)
    if not range_header:
        resp = send_file(fullname, mimetype=get_mime_type(fullname))
        resp.make_conditional(request)
        return resp

    size = os.path.getsize(fullname)
    byte1, byte2 = 0, None

    m = re.search(r'(\d+)-(\d*)', range_header)
    g = m.groups()

    if g[0]:
        byte1 = int(g[0])
    if g[1]:
        byte2 = int(g[1])

    length = size - byte1
    if byte2 is not None:
        length = min(byte2 - byte1 + 1, length)

    resp = Response(send_chunks_iter(fullname, byte1, length),
                    206,
                    mimetype=get_mime_type(fullname),
                    direct_passthrough=True)
    resp.headers.add('Content-Range',
                     'bytes {0}-{1}/{2}'.format(byte1,
                                                byte1 + length - 1,
                                                size)
                     )
    return resp


@app.route('/', defaults={'path': ''}, methods=["GET", "POST"])
@app.route('/<path:path>', methods=["GET", "POST"])
@redirect_on_exception
def handle(path):
    user, password, cred = get_user(path)

    fullname, dirname, fname = normalize_path(path)

    action = request.values.get("action", None)

    if fname is None and action in [None, "list"]:
        if not (has_access(user, dirname, Access.LIST)
                or has_access(user, dirname, Access.UPLOAD)
                or has_access(user, dirname, Access.MKDIR)):
            raise AccessError(HTTPStatus.UNAUTHORIZED, "forbidden")

        subdirs, files = list_dir(fullname)
        filter_file_list(user, dirname, subdirs, files)
        status = request.values.get("status", None)
        resp = Response(
            render_template_string(
                TEMPLATE,
                dirname=dirname,
                parentdir=None if not dirname else os.path.split(dirname)[0],
                prefix=mk_prefix(),
                files=[{"name": f,
                        "path": os.path.join(dirname, f)}
                       for f in files],
                subdirs=[{"name": s,
                          "path": os.path.join(dirname, s)}
                         for s in subdirs],
                status=status,
                allow_upload=has_access(user, dirname, Access.UPLOAD),
                allow_delete=has_access(user, dirname, Access.DELETE),
                allow_fetch=has_access(user, dirname, Access.FETCH),
                allow_mkdir=has_access(user, dirname, Access.MKDIR),
                debug=config.getboolean("global", "debug"),
                user=user)
            )

    elif fname is None and action == "upload":
        if has_access(user, dirname, Access.UPLOAD):
            resp = upload_file(user, dirname)
        else:
            raise AccessError(403, "forbidden")

    elif fname and action == "delete":
        if has_access(user, dirname, Access.DELETE):
            resp = delete_file(user, dirname, fname)
        else:
            raise AccessError(403, "forbidden")

    elif fname is None and action == "mkdir":
        if has_access(user, dirname, Access.MKDIR):
            resp = make_dir(user, dirname)
        else:
            raise AccessError(403, "forbidden")

    elif fname and action is None:
        if has_access(user, dirname, Access.FETCH):
            logging.getLogger(__name__).info("%s - - Download %s by %s",
                                             request.remote_addr,
                                             fullname,
                                             user)
            resp = streamfile(fullname)
        else:
            raise AccessError(403, "forbidden")

    resp.set_cookie("cred", cred)
    return resp

#
# FTP adapter
#


class MyAuthorizer(object):

    def validate_authentication(self, username, password, handler):
        creds = config.user_perms.get(username, dict()).get("creds", None)
        if creds != password:
            logging.warning("Invalid credentials for user %s", username)
            raise AuthenticationFailed()
        logging.info("User %s logged in", username)

    def get_home_dir(self, username):
        return config.get("global", "basedir")

    def get_msg_login(self, username):
        return "Welcome, %s." % username

    def get_msg_quit(self, username):
        return "Bye."

    def impersonate_user(self, username, password):
        pass

    def terminate_impersonation(self, username):
        pass

    def has_user(self, username):
        return True

    def get_perms(self, username):
        return "l"

    def has_perm(self, username, perm, path=None):
        if path and path.startswith(config.get("global", "basedir")):
            path = path[len(config.get("global", "basedir")):]
        path = path.lstrip(os.path.sep)

        if perm in "elmrwd":
            logging.debug("User %s granted %s for '%s'",
                          username,
                          perm,
                          path)
            return True
        else:
            logging.warn("Perm %s invalid for user %s at '%s'",
                         perm,
                         username,
                         path)
        return False


class MyFilesystem(AbstractedFS):

    def strip_path(self, path):
        if path.startswith(config.get("global", "basedir")):
            path = path[len(config.get("global", "basedir")):]
        return path.lstrip(os.path.sep)

    def has_access(self, path, access_list):
        path = self.strip_path(path)
        user = self.cmd_channel.username

        if isinstance(access_list, Access):
            access_list = [access_list]
        for access in access_list:
            try:
                if has_access(user, path, access):
                    logging.info("%s granted to %s for '%s'",
                                 access, user, path)
                    return True
                logging.debug("%s not allowed for %s at '%s'",
                              access, user, path)
            except AccessError:
                pass
        logging.warning("%s not allowed for %s at '%s'",
                        access_list, user, path)
        return False

    def get_user_by_uid(self, uid):
        return "fus"

    def get_group_by_gid(self, gid):
        return "fus"

    def chdir(self, path):
        if self.has_access(path, [Access.LIST, Access.MKDIR, Access.UPLOAD]):
            return AbstractedFS.chdir(self, path)
        else:
            raise FilesystemError("invalid path")

    def mkdir(self, path):
        if self.has_access(path, Access.MKDIR):
            return AbstractedFS.mkdir(self, path)
        else:
            raise FilesystemError("invalid path")

    def listdir(self, path):
        user = self.cmd_channel.username
        if self.has_access(path, Access.LIST):
            dirs, files = list_dir(path)
            basepath = self.strip_path(path)
            filter_file_list(user, basepath, dirs, files)
            return dirs + files
        else:
            raise FilesystemError("invalid path")

    def remove(self, path):
        if self.has_access(path, Access.DELETE):
            return AbstractedFS.remove(self, path)
        else:
            raise FilesystemError("invalid path")

    def rename(self, src, dst):
        raise FilesystemError("invalid path")

    def chmod(self, path, mode):
        raise FilesystemError("invalid path")

    def open(self, filename, mode):
        path, name = os.path.split(filename)
        name = text.get_valid_filename(name)

        if "w" in mode:
            if self.has_access(path, Access.UPLOAD):
                return AbstractedFS.open(self, filename, mode)
            else:
                raise FilesystemError("invalid path")
        else:
            if self.has_access(path, Access.FETCH):
                return AbstractedFS.open(self, filename, mode)
            else:
                raise FilesystemError("invalid path")

    def mkstemp(self, suffix='', prefix='', path=None, mode='wb'):
        raise FilesystemError("invalid path")


def make_ftp_server():

    class DummyFtpServer:
        """Just a mock to make the FTP code happy when
        there is no server available"""

        def serve_forever(self, *args, **kwargs):
            pass

        def close_all(self, *args, **kwargs):
            pass

    try:
        ftp_handler = FTPHandler
        ftp_handler.authorizer = MyAuthorizer()
        ftp_handler.abstracted_fs = MyFilesystem

        ftp_handler.banner = "fus at your service."
        address = (config.get("global", "host"),
                   config.getint("global", "ftp_port"))
        ftp_server = FTPServer(address, ftp_handler)
        ftp_server.set_reuse_addr()

        return ftp_server
    except NoOptionError:
        logging.warning("Not running FTP server, is 'ftp_port' configured?")
    except Exception:
        logging.warning("Not running FTP server, is pyftpdlib available?")

    return DummyFtpServer()


ftp_server = make_ftp_server()

ftp_thread = threading.Thread(target=ftp_server.serve_forever, args=(1,))
ftp_thread.start()

http_server = run_server(app)
while True:
    try:
        gevent.sleep(60)
    except KeyboardInterrupt:
        break

logging.getLogger(__name__).info("HTTP Server stopped")
ftp_server.close_all()
ftp_thread.join()
logging.getLogger(__name__).info("FTP Server stopped")
