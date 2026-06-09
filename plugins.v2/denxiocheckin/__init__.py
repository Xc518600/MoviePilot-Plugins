# -*- coding: utf-8 -*-
"""Denxio 签到插件。"""

import json
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType


class DenxioCheckin(_PluginBase):
    plugin_name = "Denxio签到"
    plugin_desc = "登录 Denxio 后执行赞助签到。"
    plugin_version = "1.2"
    plugin_author = "老公"
    plugin_description = "适配 api.denxio.top：使用邮箱/密码登录，然后执行 tbe-sponsor-checkin 签到流程。"
    plugin_icon = "check_circle.png"
    plugin_config_prefix = "denxiocheckin_"
    plugin_level = 1
    auth_level = 1

    _enabled = False
    _notify = True
    _run_once = False
    _base_url = "https://api.denxio.top"
    _cron = "5 0 * * *"
    _email = ""
    _password = ""
    _cookie = ""
    _headers_json = "{}"
    _timeout = 15
    _timezone = "Asia/Shanghai"

    _last_run_at = ""
    _last_success = False
    _last_message = ""
    _last_status_code = 0
    _last_response = ""

    _scheduler: Optional[BackgroundScheduler] = None
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._notify = bool(config.get("notify", True))
            self._run_once = bool(config.get("run_once", False))
            self._base_url = (config.get("base_url") or self._base_url).strip().rstrip("/")
            self._cron = (config.get("cron") or self._cron).strip()
            self._email = (config.get("email") or "").strip()
            self._password = config.get("password") or ""
            self._cookie = config.get("cookie") or ""
            self._headers_json = config.get("headers_json") or "{}"
            self._timeout = max(int(config.get("timeout") or 15), 3)
            self._timezone = (config.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"
            self._last_run_at = config.get("last_run_at") or ""
            self._last_success = bool(config.get("last_success", False))
            self._last_message = config.get("last_message") or ""
            self._last_status_code = int(config.get("last_status_code") or 0)
            self._last_response = config.get("last_response") or ""

        if self._enabled:
            self._start_scheduler()

        if self._run_once:
            logger.info("Denxio签到插件收到立即运行请求")
            self._run_once = False
            self.__save_state()
            threading.Thread(target=self._checkin, kwargs={"manual": True}, daemon=True).start()

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run",
                "summary": "立即签到",
                "description": "手动触发一次 Denxio 签到。",
                "methods": ["POST"],
            }
        ]

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None

    def _start_scheduler(self):
        try:
            scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Shanghai"))
            trigger = CronTrigger.from_crontab(self._cron, timezone=pytz.timezone("Asia/Shanghai"))
            scheduler.add_job(self._checkin, trigger=trigger, id="denxio_checkin", replace_existing=True)
            scheduler.start()
            self._scheduler = scheduler
            logger.info(f"Denxio签到定时任务已启动，cron={self._cron}")
        except Exception as e:
            logger.error(f"Denxio签到定时任务启动失败：{e}")

    def _parse_json_object(self, raw: str, field_name: str) -> Dict[str, Any]:
        raw = (raw or "").strip() or "{}"
        try:
            value = json.loads(raw)
        except Exception as e:
            raise ValueError(f"{field_name} 不是合法 JSON：{e}")
        if not isinstance(value, dict):
            raise ValueError(f"{field_name} 必须是 JSON 对象")
        return value

    def _build_session_headers(self) -> Dict[str, Any]:
        headers = self._parse_json_object(self._headers_json, "请求头")
        if self._cookie:
            headers["Cookie"] = self._cookie
        headers.setdefault("User-Agent", "MoviePilot-DenxioCheckin/1.1")
        headers.setdefault("Content-Type", "application/json")
        return headers

    def _login(self, session: requests.Session) -> Dict[str, Any]:
        if not self._email or not self._password:
            raise ValueError("请先填写邮箱和密码")
        login_url = f"{self._base_url}/api/v1/auth/login"
        resp = session.post(login_url, json={"email": self._email, "password": self._password}, timeout=self._timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"登录失败，HTTP {resp.status_code}：{resp.text[:300]}")
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("message") or "登录失败")
        data = payload.get("data") or {}
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError("登录成功但未返回 access_token")
        session.headers["Authorization"] = f"Bearer {access_token}"
        return data

    def _get_status(self, session: requests.Session) -> Dict[str, Any]:
        url = f"{self._base_url}/api/v1/tbe-sponsor-checkin/status"
        resp = session.get(url, params={"timezone": self._timezone}, timeout=self._timeout)
        data = self._unwrap_api(resp)
        return data if isinstance(data, dict) else {}

    def _begin_normal(self, session: requests.Session) -> Dict[str, Any]:
        url = f"{self._base_url}/api/v1/tbe-sponsor-checkin/normal/begin"
        resp = session.post(url, json={"timezone": self._timezone}, timeout=self._timeout)
        return self._unwrap_api(resp, allow_409=True)

    def _claim_normal(self, session: requests.Session, token: str) -> Dict[str, Any]:
        url = f"{self._base_url}/api/v1/tbe-sponsor-checkin/normal/claim"
        resp = session.post(url, json={"token": token, "timezone": self._timezone}, timeout=self._timeout)
        data = self._unwrap_api(resp)
        return data if isinstance(data, dict) else {}

    def _unwrap_api(self, resp: requests.Response, allow_409: bool = False):
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = None
        if resp.status_code == 409 and allow_409 and isinstance(payload, dict):
            return payload
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}：{text[:300]}")
        if isinstance(payload, dict):
            if payload.get("code") == 0:
                return payload.get("data")
            raise RuntimeError(payload.get("message") or text[:300] or "接口请求失败")
        raise RuntimeError(text[:300] or "接口未返回 JSON")

    def _checkin(self, manual: bool = False):
        with self._lock:
            run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                session = requests.Session()
                session.headers.update(self._build_session_headers())
                self._login(session)
                before_status = self._get_status(session)
                if before_status.get("normal_done"):
                    record = ((before_status.get("recent_records") or [{}])[0]) if isinstance(before_status.get("recent_records"), list) else {}
                    amount = record.get("amount")
                    sponsor_name = record.get("sponsor_name") or "-"
                    message = f"今天已经签到过了，赞助商：{sponsor_name}，奖励：{amount if amount is not None else '-'}"
                    self._save_result(run_at, True, 200, message, json.dumps(before_status, ensure_ascii=False)[:2000])
                    self._notify_result(manual, True, 200, message, run_at)
                    return

                begin_result = self._begin_normal(session)
                if isinstance(begin_result, dict) and begin_result.get("code") == 409:
                    message = begin_result.get("message") or "今天已经签到过了"
                    self._save_result(run_at, True, 409, message, json.dumps(begin_result, ensure_ascii=False)[:2000])
                    self._notify_result(manual, True, 409, message, run_at)
                    return

                token = (begin_result or {}).get("token") if isinstance(begin_result, dict) else None
                if not token:
                    raise RuntimeError(f"开始签到成功，但没有拿到 token：{begin_result}")
                claim_result = self._claim_normal(session, token)
                amount = claim_result.get("amount") if isinstance(claim_result, dict) else None
                sponsor_name = claim_result.get("sponsor_name") if isinstance(claim_result, dict) else None
                message = f"签到成功，赞助商：{sponsor_name or '-'}，奖励：{amount if amount is not None else '-'}"
                self._save_result(run_at, True, 200, message, json.dumps(claim_result, ensure_ascii=False)[:2000])
                self._notify_result(manual, True, 200, message, run_at)
            except Exception as e:
                self._save_result(run_at, False, 0, str(e), "")
                logger.error(f"Denxio签到执行失败：{e}")
                self._notify_result(manual, False, 0, str(e), run_at)

    def _save_result(self, run_at: str, success: bool, status_code: int, message: str, response_text: str):
        self._last_run_at = run_at
        self._last_success = success
        self._last_message = message
        self._last_status_code = status_code
        self._last_response = response_text[:2000]
        self.__save_state()
        level = logger.info if success else logger.warning
        level(f"Denxio签到{'成功' if success else '失败'}：status={status_code}, message={message}")

    def _notify_result(self, manual: bool, success: bool, status_code: int, message: str, run_at: str):
        if not self._notify:
            return
        self.post_message(
            title="Denxio签到结果" if success else "Denxio签到失败",
            mtype=NotificationType.Plugin,
            text=f"时间：{run_at}\n方式：{'手动' if manual else '定时'}\n结果：{'成功' if success else '失败'}\n状态码：{status_code or '-'}\n说明：{message}",
        )

    def __save_state(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "run_once": False,
            "base_url": self._base_url,
            "cron": self._cron,
            "email": self._email,
            "password": self._password,
            "timezone": self._timezone,
            "cookie": self._cookie,
            "headers_json": self._headers_json,
            "timeout": self._timeout,
            "last_run_at": self._last_run_at,
            "last_success": self._last_success,
            "last_message": self._last_message,
            "last_status_code": self._last_status_code,
            "last_response": self._last_response,
        })

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "run_once", "label": "保存后立即签到一次"}}]},
                            {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "默认已适配 Denxio 真实签到流程：登录 -> 查询状态 -> begin -> claim。一般只需要填写邮箱、密码并启用即可。"}}]},
                            {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextField", "props": {"model": "base_url", "label": "站点地址", "placeholder": "https://api.denxio.top", "hint": "通常不需要改，除非站点域名变化"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "email", "label": "邮箱", "placeholder": "请输入登录邮箱"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "password", "label": "密码", "type": "password", "placeholder": "请输入密码"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "cron", "label": "Cron 表达式", "placeholder": "5 0 * * *", "hint": "默认每天 00:05 执行一次"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "timezone", "label": "时区", "placeholder": "Asia/Shanghai", "hint": "默认按中国时区签到"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "timeout", "label": "超时秒数", "type": "number", "placeholder": "15", "hint": "网络一般正常时 15 秒足够"}}]},
                            {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextarea", "props": {"model": "cookie", "label": "Cookie（通常留空）", "rows": 2, "placeholder": "name=value; name2=value2", "hint": "当前真实流程只靠登录 token 就能签到，除非站点后续风控变化，否则不用填"}}]},
                            {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextarea", "props": {"model": "headers_json", "label": "附加请求头(JSON，可留空)", "rows": 4, "placeholder": '{\n  "X-Test": "value"\n}', "hint": "保留给以后兼容风控或特殊请求头，当前通常不需要填写"}}]},
                            {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VAlert", "props": {"type": self._alert_type(), "variant": "tonal", "text": self._status_text()}}]},
                        ],
                    }
                ],
            }
        ], {
            "enabled": self._enabled,
            "notify": self._notify,
            "run_once": False,
            "base_url": self._base_url,
            "cron": self._cron,
            "email": self._email,
            "password": self._password,
            "timezone": self._timezone,
            "cookie": self._cookie,
            "headers_json": self._headers_json,
            "timeout": self._timeout,
        }

    def _status_text(self) -> str:
        if not self._last_run_at:
            return "最近未执行签到。已适配真实站点流程：/api/v1/auth/login -> /api/v1/tbe-sponsor-checkin/status -> /normal/begin -> /normal/claim。"
        return (
            f"最近执行：{self._last_run_at} | "
            f"结果：{'成功' if self._last_success else '失败'} | "
            f"状态码：{self._last_status_code or '-'} | "
            f"说明：{self._last_message or '-'}"
        )

    def _alert_type(self) -> str:
        if not self._last_run_at:
            return "info"
        return "success" if self._last_success else "warning"
