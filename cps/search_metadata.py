# -*- coding: utf-8 -*-

#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2021 OzzieIsaacs
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program. If not, see <http://www.gnu.org/licenses/>.

import concurrent.futures
import importlib
import inspect
import json
import os
import sys
from urllib.parse import urlsplit

from flask import Blueprint, request, url_for, make_response, jsonify
from .cw_login import current_user
from flask_babel import get_locale
from sqlalchemy.exc import InvalidRequestError, OperationalError
from sqlalchemy.orm.attributes import flag_modified

from cps.services.Metadata import Metadata
from . import constants, logger, ub, web_server
from .usermanagement import user_login_required


meta = Blueprint("metadata", __name__)

log = logger.create()


# <metadata-cover-proxy-patch>
# 元数据预览封面代理: 浏览器直连图片站会被防盗链 + CSP 拦 (Douban 只认自家 Referer),
# 因此改由服务端代取。见 patch_metadata_cover_proxy.py 顶部说明。
MAX_PREVIEW_COVER_BYTES = 10 * 1024 * 1024


def _preview_cover_headers(url):
    """与 cps.helper 中同一套 Referer 规则, 复用避免两处逻辑漂移。"""
    try:
        from .helper import _cover_referer_headers
        headers = _cover_referer_headers(url)
        if headers:
            return headers
    except Exception:
        pass
    # helper 补丁缺失时的兜底, 规则与之一致
    try:
        parts = urlsplit(url)
        host = (parts.netloc or "").lower()
        if parts.scheme and host:
            if host.endswith("doubanio.com") or host.endswith("douban.com"):
                return {"Referer": "https://book.douban.com/"}
            return {"Referer": "{0}://{1}/".format(parts.scheme, parts.netloc)}
    except Exception:
        pass
    return {}


def _fetch_preview_cover(url):
    """服务端取图。优先走 cw_advocate 以复用其 SSRF 防护。"""
    headers = _preview_cover_headers(url)
    try:
        from . import cw_advocate
        return cw_advocate.get(url, timeout=(10, 20), allow_redirects=False, headers=headers)
    except ImportError:
        import requests
        return requests.get(url, timeout=(10, 20), allow_redirects=False, headers=headers)


@meta.route("/metadata/cover")
@user_login_required
def metadata_cover():
    url = _safe_metadata_url(request.args.get("url", ""))
    if not url.startswith(("http://", "https://")):
        return make_response("", 400)
    try:
        upstream = _fetch_preview_cover(url)
    except Exception as exc:
        log.debug("preview cover fetch failed for %s: %s", url, exc)
        return make_response("", 502)
    if upstream.status_code != 200:
        log.debug("preview cover upstream %s for %s", upstream.status_code, url)
        return make_response("", 502)
    content_type = (upstream.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        return make_response("", 415)
    body = upstream.content
    if len(body) > MAX_PREVIEW_COVER_BYTES:
        return make_response("", 413)
    response = make_response(body)
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


def _safe_metadata_url(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme in ("http", "https"):
        return value
    if not parsed.scheme and value.startswith("/"):
        return value
    return ""


def _sanitize_metadata_record(record):
    record["url"] = _safe_metadata_url(record.get("url"))
    record["cover"] = _safe_metadata_url(record.get("cover"))
    # <metadata-cover-proxy-patch>
    # ★ 只写 cover_preview, 绝不覆盖 cover: cover 同时是 book_edit 表单
    #   「Fetch Cover from URL」的值 (get_meta.js populateForm), 覆盖它会把
    #   站内相对路径送进 helper.save_cover_from_url() -> InvalidSchema 500
    #   (2026-09-28 事故: calibre-web.log 里 "No connection adapters were found")。
    record["cover_preview"] = record["cover"]
    if record["cover"].startswith(("http://", "https://")):
        record["cover_preview"] = url_for("metadata.metadata_cover", url=record["cover"])
    source = record.get("source")
    if isinstance(source, dict):
        source["link"] = _safe_metadata_url(source.get("link"))
    return record


def _serialize_metadata_records(records):
    return [
        _sanitize_metadata_record(asdict(record))
        for record in (records or [])
        if record
    ]

try:
    from dataclasses import asdict
except ImportError:
    log.info('*** "dataclasses" is needed for calibre-web to run. Please install it using pip: "pip install dataclasses" ***')
    print('*** "dataclasses" is needed for calibre-web to run. Please install it using pip: "pip install dataclasses" ***')
    web_server.stop(True)
    sys.exit(6)

new_list = list()
meta_dir = os.path.join(constants.BASE_DIR, "cps", "metadata_provider")
modules = os.listdir(os.path.join(constants.BASE_DIR, "cps", "metadata_provider"))
for f in modules:
    if os.path.isfile(os.path.join(meta_dir, f)) and not f.endswith("__init__.py"):
        a = os.path.basename(f)[:-3]
        try:
            importlib.import_module("cps.metadata_provider." + a)
            new_list.append(a)
        except (IndentationError, SyntaxError) as e:
            log.error("Syntax error for metadata source: {} - {}".format(a, e))
        except ImportError as e:
            log.debug("Import error for metadata source: {} - {}".format(a, e))


def list_classes(provider_list):
    classes = list()
    for element in provider_list:
        for name, obj in inspect.getmembers(
            sys.modules["cps.metadata_provider." + element]
        ):
            if (
                inspect.isclass(obj)
                and name != "Metadata"
                and issubclass(obj, Metadata)
            ):
                classes.append(obj())
    return classes


cl = list_classes(new_list)


@meta.route("/metadata/provider")
@user_login_required
def metadata_provider():
    active = current_user.view_settings.get("metadata", {})
    provider = list()
    for c in cl:
        ac = active.get(c.__id__, True)
        provider.append(
            {"name": c.__name__, "active": ac, "initial": ac, "id": c.__id__}
        )
    return make_response(jsonify(provider))


@meta.route("/metadata/provider", methods=["POST"])
@meta.route("/metadata/provider/<prov_name>", methods=["POST"])
@user_login_required
def metadata_change_active_provider(prov_name):
    new_state = request.get_json()
    active = current_user.view_settings.get("metadata", {})
    active[new_state["id"]] = new_state["value"]
    current_user.view_settings["metadata"] = active
    try:
        try:
            flag_modified(current_user, "view_settings")
        except AttributeError:
            pass
        ub.session.commit()
    except (InvalidRequestError, OperationalError):
        log.error("Invalid request received: {}".format(request))
        return "Invalid request", 400
    if "initial" in new_state and prov_name:
        data = []
        provider = next((c for c in cl if c.__id__ == prov_name), None)
        if provider is not None:
            data = provider.search(new_state.get("query", ""))
        return make_response(jsonify(_serialize_metadata_records(data)))
    return ""


@meta.route("/metadata/search", methods=["POST"])
@user_login_required
def metadata_search():
    query = request.form.to_dict().get("query")
    data = list()
    active = current_user.view_settings.get("metadata", {})
    locale = get_locale()
    if query:
        static_cover = url_for("static", filename="generic_cover.jpg")
        # ret = cl[0].search(query, static_cover, locale)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            meta = {
                executor.submit(c.search, query, static_cover, locale): c
                for c in cl
                if active.get(c.__id__, True)
            }
            for future in concurrent.futures.as_completed(meta):
                data.extend(_serialize_metadata_records(future.result()))
    return make_response(jsonify(data))
