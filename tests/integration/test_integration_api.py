"""Этап 8: API для бота игр и доставка событий outbox (подпись, ретраи, идемпотентность)."""
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.config import Settings
from app.db.models import Checkin, CheckinMethod, EventDay, OutboundEvent, OutboxStatus, Participant, Team
from app.db.session import create_sessionmaker
from app.services import outbox
from app.services.outbox_delivery import deliver_pending, sign
from app.web import api
from app.web.keys import SESSIONMAKER, SETTINGS

TOKEN = "integration-token"
SECRET = "webhook-secret"


@pytest.fixture
async def world(session):
    team = Team(name="Rockets")
    session.add(team)
    await session.flush()
    p = Participant(email="aru@x.kz", full_name="Aru", invite_token="ARUtoken0001", team_id=team.id, telegram_user_id=1001)
    session.add(p)
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Almaty")).date()
    day = EventDay(date=today, label="День 1", checkin_opens_at=datetime.now(UTC), checkin_closes_at=datetime.now(UTC))
    session.add(day)
    await session.flush()
    session.add(Checkin(participant_id=p.id, event_day_id=day.id, method=CheckinMethod.SELF_QR))
    await session.commit()
    return p


async def _client(engine, settings) -> TestClient:
    app = web.Application()
    app[SETTINGS], app[SESSIONMAKER] = settings, create_sessionmaker(engine)
    api.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_api_requires_token_and_reports_status(engine, world):
    client = await _client(engine, Settings(_env_file=None, integration_token=TOKEN))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    try:
        assert (await client.get("/api/participant-status?telegram_user_id=1001")).status == 401
        assert (await client.get("/api/participant-status?telegram_user_id=1001", headers={"Authorization": "Bearer x"})).status == 401

        data = await (await client.get("/api/participant-status?telegram_user_id=1001", headers=auth)).json()
        assert data["activated"] is True and data["team"] == "Rockets" and data["checked_in_today"] is True

        data = await (await client.get("/api/participant-status?telegram_user_id=555", headers=auth)).json()
        assert data == {"telegram_user_id": 555, "activated": False, "checkins": [], "checked_in_today": False}

        data = await (await client.get("/api/checkin-status?telegram_user_id=1001", headers=auth)).json()
        assert data["checked_in"] is True
        data = await (await client.get("/api/checkin-status?telegram_user_id=1001&day=2020-01-01", headers=auth)).json()
        assert data["checked_in"] is False
        assert (await client.get("/api/checkin-status?telegram_user_id=abc", headers=auth)).status == 400
    finally:
        await client.close()


class Receiver:
    """Поддельный бот игр: проверяет подпись и умеет «лежать»."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.down = False

    async def handle(self, request: web.Request) -> web.Response:
        if self.down:
            return web.Response(status=503)
        body = await request.read()
        ts = request.headers["X-BC-Timestamp"]
        expected = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers["X-BC-Signature"]):
            return web.Response(status=401)
        self.received.append(await request.json())
        return web.json_response({"ok": True})


@pytest.fixture
async def receiver():
    r = Receiver()
    app = web.Application()
    app.router.add_post("/bc-events", r.handle)
    server = TestServer(app)
    await server.start_server()
    r.url = str(server.make_url("/bc-events"))
    yield r
    await server.close()


async def test_outbox_delivers_signed_and_retries_when_receiver_down(engine, session, world, receiver):
    settings = Settings(_env_file=None, gamification_webhook_url=receiver.url, gamification_webhook_secret=SECRET)
    sm = create_sessionmaker(engine)
    outbox.enqueue(session, outbox.CHECKIN_CREATED, {"telegram_user_id": 1001, "day": "2026-11-14"})
    await session.commit()

    receiver.down = True
    async with aiohttp.ClientSession() as http:
        delivered, dead = await deliver_pending(sm, settings, http)
    assert (delivered, dead, receiver.received) == (0, [], [])
    session.expire_all()  # событие менял воркер в своей сессии
    event = await session.scalar(select(OutboundEvent))
    assert event.status == OutboxStatus.PENDING and event.attempts == 1 and event.next_attempt_at > datetime.now(UTC)

    # Пока не наступило время повтора — не шлём. Потом получатель поднялся — доставили ровно один раз.
    receiver.down = False
    async with aiohttp.ClientSession() as http:
        assert (await deliver_pending(sm, settings, http))[0] == 0
        event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        assert (await deliver_pending(sm, settings, http))[0] == 1
        assert (await deliver_pending(sm, settings, http))[0] == 0
    assert len(receiver.received) == 1
    assert receiver.received[0]["type"] == "checkin.created" and receiver.received[0]["data"]["telegram_user_id"] == 1001


async def test_outbox_marks_dead_after_a_day(engine, session, receiver):
    settings = Settings(_env_file=None, gamification_webhook_url=receiver.url, gamification_webhook_secret=SECRET)
    event = outbox.enqueue(session, outbox.PARTICIPANT_LINKED, {"telegram_user_id": 1})
    await session.flush()
    event.created_at = datetime.now(UTC) - timedelta(days=2)
    await session.commit()
    receiver.down = True
    async with aiohttp.ClientSession() as http:
        _, dead = await deliver_pending(create_sessionmaker(engine), settings, http)
    assert len(dead) == 1


def test_signature_format():
    assert sign("k", "1", b"{}") == "sha256=" + hmac.new(b"k", b"1.{}", hashlib.sha256).hexdigest()
