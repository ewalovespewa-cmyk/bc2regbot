"""Поддельный Telegram Bot API для нагрузочных прогонов (без интернета и без настоящего бота).

Отвечает на POST /bot<token>/<method> как настоящий API и записывает все отправки.
Умеет имитировать 429 (каждый N-й запрос) и 403 (заблокировавшие бота chat_id).

Запуск отдельно:  python -m scripts.fake_telegram --port 8081 --every-429 50 --blocked 2001,2002
Статистика:       GET http://127.0.0.1:8081/stats
"""
import argparse
import asyncio
import itertools
import json
import time
from collections import Counter, defaultdict

from aiohttp import web


class FakeTelegram:
    def __init__(self, every_429: int = 0, blocked: set[int] | None = None, retry_after: int = 1) -> None:
        self.every_429 = every_429
        self.blocked = blocked or set()
        self.retry_after = retry_after
        self.requests = itertools.count(1)
        self.message_ids = itertools.count(1)
        self.sent: dict[int, list[str]] = defaultdict(list)  # chat_id → тексты
        self.methods: Counter[str] = Counter()
        self.errors_429 = 0
        self.errors_403 = 0

    async def handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        self.methods[method] += 1
        params = await self._params(request)
        if method == "getMe":
            return self._ok({"id": 123456, "is_bot": True, "first_name": "LoadBot", "username": "load_test_bot"})

        chat_id = params.get("chat_id")
        n = next(self.requests)
        if method.startswith("send") and self.every_429 and n % self.every_429 == 0:
            self.errors_429 += 1
            return web.json_response(
                {"ok": False, "error_code": 429, "description": f"Too Many Requests: retry after {self.retry_after}",
                 "parameters": {"retry_after": self.retry_after}},
                status=429,  # как настоящий API: HTTP-статус равен error_code
            )
        if method.startswith("send") and chat_id is not None and int(chat_id) in self.blocked:
            self.errors_403 += 1
            return web.json_response(
                {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}, status=403
            )
        if method.startswith("send") or method.startswith("edit") or method == "copyMessage":
            text = params.get("text") or params.get("caption") or ""
            if chat_id is not None:
                self.sent[int(chat_id)].append(text)
            return self._ok(
                {"message_id": next(self.message_ids), "date": int(time.time()),
                 "chat": {"id": int(chat_id or 0), "type": "private" if int(chat_id or 0) > 0 else "supergroup"},
                 "text": text}
            )
        return self._ok(True)

    async def stats(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"methods": self.methods, "chats": len(self.sent), "messages": sum(len(v) for v in self.sent.values()),
             "errors_429": self.errors_429, "errors_403": self.errors_403}
        )

    @staticmethod
    async def _params(request: web.Request) -> dict:
        if request.content_type == "application/json":
            return await request.json()
        data = await request.post()
        out = {}
        for key, value in data.items():
            if isinstance(value, str) and value[:1] in "{[":
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            out[key] = value
        return out

    @staticmethod
    def _ok(result) -> web.Response:
        return web.json_response({"ok": True, "result": result})

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self.handle)
        app.router.add_get("/stats", self.stats)
        return app


async def start(port: int, **kwargs) -> tuple[FakeTelegram, web.AppRunner]:
    fake = FakeTelegram(**kwargs)
    runner = web.AppRunner(fake.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return fake, runner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--every-429", type=int, default=0, help="каждый N-й send* отвечает 429")
    parser.add_argument("--blocked", default="", help="chat_id через запятую, которые «заблокировали бота»")
    args = parser.parse_args()
    blocked = {int(x) for x in args.blocked.split(",") if x}

    async def run() -> None:
        await start(args.port, every_429=args.every_429, blocked=blocked)
        print(f"Fake Telegram API: http://127.0.0.1:{args.port}  (статистика: /stats)")
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
