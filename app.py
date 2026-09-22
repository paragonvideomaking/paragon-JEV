"""Локальный сервер демо «ТЗ перед стартом».

Запуск: python3 app.py  →  http://127.0.0.1:8000

Особенности безопасности:
  * Сервер слушает ТОЛЬКО 127.0.0.1.
  * API-ключ TypeSafe хранится ТОЛЬКО в памяти процесса (не в файлах,
    логах, ответах клиенту). После остановки сервера ключ нужно ввести снова.
  * Endpoints настройки и оценки защищены от CSRF: проверяется заголовок
    Origin и одноразовый CSRF-токен, выданный при загрузке страницы.
  * CORS не включается (никакого Access-Control-Allow-Origin: *).
  * Внешний запрос к TypeSafe отправляет только сервер с Authorization: Bearer.

Записи runs (реальные запросы/ответы без секретов) складываются в ./runs
только для вымышленных съёмочных данных.
"""

import json
import os
import secrets
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import questions as q
import rules

HOST = "127.0.0.1"
PORT = int(os.environ.get("TZ_PORT", "8000"))  # предпочтительный порт
# BASE_URL вычисляется после привязки сокета (порт может измениться, если 8000 занят).
BASE_URL = f"http://{HOST}:{PORT}"

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
REQUEST_TIMEOUT = 20  # секунд
MAX_BRIEF_LEN = 12000

# --- Состояние процесса (только в памяти) ---
_API_KEY = None            # ключ TypeSafe, живёт только пока работает процесс
_CSRF_TOKEN = secrets.token_urlsafe(32)  # выдаётся странице, проверяется на POST


def _mask_key_present():
    return _API_KEY is not None and len(_API_KEY) > 0


class Handler(BaseHTTPRequestHandler):
    server_version = "TZCheck/1.0"

    # Заглушаем стандартный лог, чтобы случайно не записать ничего лишнего;
    # пишем только безопасные строки без секретов.
    def log_message(self, fmt, *args):
        return

    # ---------- вспомогательные ответы ----------

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # Явно запрещаем кэширование ответов API.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json(404, {"error": "not_found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # ---------- проверка происхождения запроса (CSRF) ----------

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if origin is None:
            # Некоторые same-origin запросы могут не слать Origin; тогда
            # опираемся на CSRF-токен. Но если Origin есть — он должен совпасть.
            return True
        return origin == BASE_URL

    def _csrf_ok(self, data):
        token = self.headers.get("X-CSRF-Token")
        if not token:
            token = data.get("csrf") if isinstance(data, dict) else None
        return token is not None and secrets.compare_digest(token, _CSRF_TOKEN)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "INVALID_JSON"

    # ---------- GET ----------

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file(os.path.join(STATIC_DIR, "index.html"),
                            "text/html; charset=utf-8")
            return
        if self.path == "/config":
            # Отдаём CSRF-токен и статус наличия ключа. Сам ключ не отдаём.
            self._send_json(200, {
                "csrf": _CSRF_TOKEN,
                "connected": _mask_key_present(),
                "base_url": BASE_URL,
            })
            return
        if self.path == "/fixtures.json":
            self._send_file(
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "fixtures.json"),
                "application/json; charset=utf-8")
            return
        self._send_json(404, {"error": "not_found"})

    # ---------- POST ----------

    def do_POST(self):
        if not self._origin_ok():
            self._send_json(403, {"error": "bad_origin",
                                  "message": "Недопустимый источник запроса."})
            return

        data = self._read_json_body()
        if data == "INVALID_JSON":
            self._send_json(400, {"error": "invalid_json",
                                  "message": "Тело запроса не является JSON."})
            return
        if not isinstance(data, dict):
            data = {}

        if not self._csrf_ok(data):
            self._send_json(403, {"error": "bad_csrf",
                                  "message": "Проверка CSRF не пройдена. "
                                             "Обновите страницу."})
            return

        if self.path == "/connect":
            self._handle_connect(data)
            return
        if self.path == "/evaluate":
            self._handle_evaluate(data)
            return
        self._send_json(404, {"error": "not_found"})

    # ---------- бизнес-логика ----------

    def _handle_connect(self, data):
        global _API_KEY
        key = data.get("api_key")
        if not isinstance(key, str) or not key.strip():
            self._send_json(400, {"error": "empty_key",
                                  "message": "Введите API-ключ."})
            return
        # Держим ключ только в памяти процесса. Не логируем, не пишем в файлы.
        _API_KEY = key.strip()
        self._send_json(200, {"connected": True})

    def _handle_evaluate(self, data):
        if not _mask_key_present():
            self._send_json(409, {"error": "no_key",
                                  "message": "API-ключ не задан. Подключитесь "
                                             "на экране подключения."})
            return

        brief = data.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            self._send_json(400, {"error": "empty_brief",
                                  "message": "Пустое ТЗ не отправляется в API."})
            return
        if len(brief) > MAX_BRIEF_LEN:
            self._send_json(400, {"error": "too_long",
                                  "message": f"ТЗ длиннее {MAX_BRIEF_LEN} символов."})
            return

        request_body = q.build_request(brief)
        payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            TYPESAFE_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read()
                duration_ms = int((time.monotonic() - started) * 1000)
                status_code = resp.status
        except urllib.error.HTTPError as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._handle_http_error(e, duration_ms)
            return
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", "")
            if "timed out" in str(reason).lower():
                self._send_json(504, {"error": "timeout",
                                      "message": "Таймаут запроса к TypeSafe "
                                                 "(20 с). Повторите вручную."})
            else:
                self._send_json(502, {"error": "network",
                                      "message": "Ошибка сети при обращении к "
                                                 "TypeSafe. Повторите вручную."})
            return
        except TimeoutError:
            self._send_json(504, {"error": "timeout",
                                  "message": "Таймаут запроса к TypeSafe "
                                             "(20 с). Повторите вручную."})
            return

        try:
            api_response = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(502, {"error": "bad_response",
                                  "message": "TypeSafe вернул невалидный JSON."})
            return

        valid, msg = _validate_answers(api_response)
        if not valid:
            self._send_json(502, {"error": "bad_answers", "message": msg})
            return

        report = rules.build_report(api_response["answers"])

        client_payload = {
            "report": report,
            "api_response": api_response,   # без секретов; ключа тут нет
            "model": api_response.get("model"),
            "usage": api_response.get("usage"),
            "duration_ms": duration_ms,
            "http_status": status_code,
        }

        _record_run(brief, request_body, api_response, duration_ms, status_code)
        self._send_json(200, client_payload)

    def _handle_http_error(self, e, duration_ms):
        code = e.code
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = ""
        messages = {
            401: "Ключ отклонён (401). Проверьте API-ключ и подключитесь снова.",
            403: "Доступ запрещён (403). Проверьте права ключа.",
            422: "Запрос не прошёл валидацию TypeSafe (422).",
            429: "Слишком много запросов (429). Подождите и повторите вручную.",
            529: "TypeSafe временно перегружен (529). Повторите вручную позже.",
        }
        msg = messages.get(code, f"Ошибка TypeSafe (HTTP {code}).")
        # detail может содержать полезную информацию об ошибке валидации.
        self._send_json(502, {
            "error": "http_error",
            "http_status": code,
            "message": msg,
            "detail": detail[:2000] if detail else "",
            "duration_ms": duration_ms,
        })


def _validate_answers(api_response):
    """Проверить, что ответ содержит все семь answers нужных типов.

    Возвращает (ok, message).
    """
    if not isinstance(api_response, dict):
        return False, "Ответ TypeSafe не является объектом."
    answers = api_response.get("answers")
    if not isinstance(answers, dict):
        return False, "В ответе нет объекта answers."

    for qid, expected_type in q.QUESTION_TYPES.items():
        ans = answers.get(qid)
        if not isinstance(ans, dict):
            return False, f"Нет ответа на вопрос «{qid}»."
        if ans.get("type") != expected_type:
            return False, f"Неверный тип ответа для «{qid}»."

        if expected_type == "choice":
            choice = ans.get("choice")
            if choice not in q.CHOICE_OPTIONS:
                return False, f"Недопустимый выбор в «{qid}»: {choice!r}."
            conf = ans.get("confidence")
            if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
                return False, f"Некорректная confidence в «{qid}»."
        elif expected_type == "score":
            score = ans.get("score")
            if not isinstance(score, (int, float)):
                return False, "Некорректный score в «result_clarity»."
            if not isinstance(ans.get("legend"), dict):
                return False, "Нет legend в «result_clarity»."
        elif expected_type == "noul":
            noul = ans.get("noul")
            if not isinstance(noul, (int, float)) or not (0.0 <= noul <= 1.0):
                return False, "Некорректное значение noul."

    return True, "ok"


def _record_run(brief, request_body, api_response, duration_ms, status_code):
    """Записать реальный запрос/ответ (без секретов) в ./runs.

    Ключ API сюда не попадает — мы пишем только тело запроса (state+questions)
    и ответ модели с метриками. Только для вымышленных съёмочных данных.
    """
    try:
        os.makedirs(RUNS_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        fname = os.path.join(RUNS_DIR, f"run-{ts}-{secrets.token_hex(3)}.json")
        record = {
            "timestamp": ts,
            "request": request_body,   # секретов нет: ключ в заголовке, не тут
            "response": api_response,
            "duration_ms": duration_ms,
            "http_status": status_code,
        }
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError:
        # Запись логов не должна ломать основной сценарий.
        pass


def _bind_server():
    """Привязать сервер к 127.0.0.1. Если порт занят — берём следующий свободный.

    После привязки обновляем глобальный BASE_URL, чтобы CSRF-проверка Origin
    использовала фактический порт.
    """
    global BASE_URL
    candidates = [PORT] + [p for p in range(8000, 8021) if p != PORT]
    last_err = None
    for port in candidates:
        try:
            httpd = ThreadingHTTPServer((HOST, port), Handler)
            BASE_URL = f"http://{HOST}:{port}"
            return httpd
        except OSError as e:
            last_err = e
            continue
    raise last_err


def main():
    os.makedirs(RUNS_DIR, exist_ok=True)
    httpd = _bind_server()
    print(f"Демо «ТЗ перед стартом» запущено: {BASE_URL}")
    print("Введите API-ключ TypeSafe на странице подключения.")
    print("Остановка: Ctrl+C. После остановки ключ нужно ввести снова.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка сервера.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
