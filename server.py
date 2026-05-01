#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


DESKTOP = Path("/Users/wuyiliu/Desktop")
HTML_FILE = DESKTOP / "新建网页.html"
HOST = "127.0.0.1"
PORT = 8765


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._serve_file(HTML_FILE)
            return
        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        if self.path == "/api/messages":
            self._handle_messages()
            return
        if self.path == "/api/reminders":
            self._handle_reminders()
            return
        self.send_error(404, "Not Found")
        return

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length)
            return json.loads(raw_body.decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": {"message": "请求体不是合法 JSON"}})
            return None

    def _handle_messages(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return

        api_url = str(payload.get("apiUrl", "")).rstrip("/")
        api_key = str(payload.get("apiKey", "")).strip()
        model = payload.get("model")
        max_tokens = payload.get("max_tokens", 4096)
        system = payload.get("system")
        messages = payload.get("messages")

        if not api_url or not api_key or not model or not system or not isinstance(messages, list):
            self._send_json(400, {"error": {"message": "缺少必要参数"}})
            return

        upstream_payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        body = json.dumps(upstream_payload, ensure_ascii=False).encode("utf-8")

        try:
            result = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "--http1.1",
                    api_url + "/v1/messages",
                    "-X",
                    "POST",
                    "-H",
                    "Content-Type: application/json; charset=utf-8",
                    "-H",
                    f"x-api-key: {api_key}",
                    "-H",
                    "anthropic-version: 2023-06-01",
                    "--data-binary",
                    "@-",
                    "-w",
                    "\n__STATUS__:%{http_code}",
                ],
                input=body,
                capture_output=True,
                timeout=120,
            )

            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                self._send_json(502, {"error": {"message": f"代理请求失败: {stderr or 'curl 执行失败'}"}})
                return

            raw_output = result.stdout
            marker = b"\n__STATUS__:"
            idx = raw_output.rfind(marker)
            if idx == -1:
                self._send_json(502, {"error": {"message": "代理请求失败: 无法解析上游响应"}})
                return

            data = raw_output[:idx]
            status_text = raw_output[idx + len(marker):].decode("utf-8", errors="replace").strip()
            status = int(status_text) if status_text.isdigit() else 502

            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as err:
            self._send_json(502, {"error": {"message": f"代理请求失败: {err}"}})

    def _handle_reminders(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return

        list_name = str(payload.get("listName", "")).strip() or "聊天任务提取器"
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            self._send_json(400, {"error": {"message": "没有可导出的任务"}})
            return

        applescript_lines = [
            'tell application "Reminders"',
            "if not (exists list " + json.dumps(list_name, ensure_ascii=False) + ") then",
            "make new list with properties {name:" + json.dumps(list_name, ensure_ascii=False) + "}",
            "end if",
        ]
        exported = 0
        skipped = []

        now = datetime.now()
        for index, item in enumerate(items, start=1):
            title = str(item.get("title", "")).strip()
            notes = str(item.get("notes", "")).strip().replace("\r\n", "\n")
            deadline = str(item.get("deadline", "")).strip()
            date_text = str(item.get("date", "")).strip()
            if not title:
                continue

            schedule = parse_schedule(deadline, date_text, now)
            if schedule is None:
                skipped.append({"index": index, "title": title, "deadline": deadline, "date": date_text})
                continue

            exported += 1
            var_name = f"dueDate{index}"
            applescript_lines.extend(build_applescript_date(var_name, schedule["start"]))
            applescript_lines.append(
                "set newReminder to make new reminder at end of reminders of list "
                + apple_string(list_name)
                + " with properties {"
                + ", ".join([
                    "name:" + apple_string(title),
                    "body:" + apple_string(notes),
                ])
                + "}"
            )
            if schedule["all_day"]:
                applescript_lines.append(f"set allday due date of newReminder to {var_name}")
            else:
                applescript_lines.append(f"set due date of newReminder to {var_name}")

        applescript_lines.append("end tell")
        script = "\n".join(applescript_lines)

        if exported == 0:
            self._send_json(400, {"error": {"message": "没有识别到可导出的日期或时间，未创建提醒事项"}, "skipped": skipped})
            return

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                message = result.stderr.strip() or result.stdout.strip() or "提醒事项导出失败"
                self._send_json(500, {"error": {"message": message}})
                return
            self._send_json(200, {"ok": True, "exported": exported, "listName": list_name, "skipped": skipped})
        except Exception as err:
            self._send_json(500, {"error": {"message": f"提醒事项导出失败: {err}"}})

    def log_message(self, format: str, *args) -> None:
        return

    def _serve_file(self, path: Path) -> None:
        if not path.exists():
            self.send_error(404, "File not found")
            return
        mime_type, _ = mimetypes.guess_type(path.name)
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(path.read_bytes())

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    if not HTML_FILE.exists():
        raise SystemExit(f"找不到文件: {HTML_FILE}")

    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Server running at {url}")
    if os.environ.get("NO_AUTO_OPEN") != "1":
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def apple_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def apple_date(value: datetime) -> str:
    return apple_string(value.strftime("%Y-%m-%d %H:%M:%S"))


def parse_schedule(deadline: str, date_text: str, now: datetime) -> dict | None:
    raw = deadline if deadline and deadline != "未指定" else date_text
    raw = raw.strip()
    if not raw or raw == "未指定":
        return None

    base_date = parse_date_only(raw, now)
    parsed_time = parse_time_only(raw)

    if base_date is None and date_text and date_text != "未指定":
        base_date = parse_date_only(date_text, now)
    if parsed_time is None and date_text and date_text != "未指定":
        parsed_time = parse_time_only(date_text)

    if base_date is None:
        return None

    if parsed_time is None:
        start = datetime(base_date.year, base_date.month, base_date.day, 9, 0, 0)
        end = start + timedelta(hours=1)
        return {"start": start, "end": end, "all_day": True}

    start = datetime(base_date.year, base_date.month, base_date.day, parsed_time[0], parsed_time[1], 0)
    end = start + timedelta(hours=1)
    return {"start": start, "end": end, "all_day": False}


def parse_date_only(text: str, now: datetime) -> datetime | None:
    s = text.strip()
    if not s:
        return None

    if "今天" in s:
        return now
    if "明天" in s:
        return now + timedelta(days=1)
    if "后天" in s:
        return now + timedelta(days=2)
    if "今晚" in s or "今晚" in s:
        return now
    if "明晚" in s:
        return now + timedelta(days=1)
    if "当晚" in s:
        return now
    if "下周一" in s:
        return next_weekday(now, 0)
    if "下周二" in s:
        return next_weekday(now, 1)
    if "下周三" in s:
        return next_weekday(now, 2)
    if "下周四" in s:
        return next_weekday(now, 3)
    if "下周五" in s:
        return next_weekday(now, 4)
    if "下周六" in s:
        return next_weekday(now, 5)
    if "下周日" in s or "下周天" in s:
        return next_weekday(now, 6)

    import re
    m = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})[./月-](\d{1,2})", s)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        year = now.year
        candidate = datetime(year, month, day)
        if candidate < now - timedelta(days=180):
            candidate = datetime(year + 1, month, day)
        return candidate
    return None


def parse_time_only(text: str) -> tuple[int, int] | None:
    s = text.strip()
    if not s:
        return None
    import re
    m = re.search(r"(\d{1,2})[:：](\d{2})", s)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        if any(token in s for token in ("下午", "晚上")) and hour < 12:
            hour += 12
        return hour, minute
    if "上午" in s:
        return 9, 0
    if "中午" in s:
        return 12, 0
    if "下午" in s:
        return 15, 0
    if "晚上" in s or "今晚" in s or "明晚" in s or "当晚" in s:
        return 19, 0
    return None


def next_weekday(now: datetime, weekday: int) -> datetime:
    days_ahead = weekday - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return now + timedelta(days=days_ahead)


def build_applescript_date(var_name: str, value: datetime) -> list[str]:
    month_name = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ][value.month - 1]
    return [
        f"set {var_name} to current date",
        f"set year of {var_name} to {value.year}",
        f"set month of {var_name} to {month_name}",
        f"set day of {var_name} to {value.day}",
        f"set hours of {var_name} to {value.hour}",
        f"set minutes of {var_name} to {value.minute}",
        f"set seconds of {var_name} to {value.second}",
    ]


if __name__ == "__main__":
    main()
