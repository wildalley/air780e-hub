"""Push engine tests.

Two things are worth stating up front, because they shape most of what is
here:

* Every provider is mocked at the httpx transport, so these tests assert on
  the *exact request* each channel makes — a wrong payload shape is a silent
  failure in production (the provider answers 200 and shows nothing).
* This engine handles verification codes, so several tests exist purely to
  keep message bodies and bot tokens out of the audit log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from hub_server.config import Settings
from hub_server.db import Database
from hub_server.main import create_app
from hub_server.notify import (
    DEFAULT_TEMPLATE,
    Notifier,
    Payload,
    _feishu_card,
    match_rules,
    render,
    scrub,
)

ICCID = "89860622180012345670"
OTHER_ICCID = "89860622180012345671"


# --------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path, agent_token="test-token")
    s.ensure_agent_token()
    return s


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "hub.db")
    yield database
    database.close()


class Recorder:
    """Captures requests and replays a scripted list of responses."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.requests: list[httpx.Request] = []
        self.scripted = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.scripted:
            # A shape every provider check accepts, so tests that only care
            # about the request need no boilerplate.
            return httpx.Response(200, json={"code": 200, "ok": True, "errcode": 0})
        answer = self.scripted.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(request.content or b"{}") for request in self.requests]


def make_notifier(db, settings, handler, **kwargs) -> Notifier:
    return Notifier(
        db,
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        backoff=(0.0, 0.0),
        **kwargs,
    )


def add_channel(
    db, *, name="推送", type="post", config=None, enabled=True
) -> int:
    if config is None:
        config = {"url": "https://sink.test/hook"}
    cursor = db.execute(
        "INSERT INTO channels (name, type, config, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, type, json.dumps(config), int(enabled),
         "2026-08-02T10:00:00+00:00"),
    )
    return int(cursor.lastrowid)


def add_rule(
    db, *, channel_id, sim_id=None, match="all", pattern="", template="",
    priority=0, enabled=True, name="",
) -> int:
    cursor = db.execute(
        "INSERT INTO rules "
        "(name, sim_id, channel_id, match, pattern, template, priority, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, sim_id, channel_id, match, pattern, template, priority, int(enabled)),
    )
    return int(cursor.lastrowid)


def add_message(
    db, *, body="验证码 123456", peer="10086", iccid=ICCID, device="a",
    is_binary=False,
) -> int:
    return db.insert_message(
        agent_id="home-arch", device=device, direction="in", peer=peer,
        body=body, ts="2026-08-02T18:00:00+08:00", iccid=iccid,
        is_binary=is_binary,
    )


def notify_logs(db) -> list[dict]:
    return db.query("SELECT * FROM notify_logs ORDER BY id")


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------


def test_render_substitutes_known_placeholders():
    text = render(
        "{card} / {sender}: {message} @ {timestamp}",
        {"card": "移动卡", "sender": "10086", "message": "hi", "timestamp": "12:00"},
    )
    assert text == "移动卡 / 10086: hi @ 12:00"


def test_render_leaves_unknown_placeholders_visible():
    """A typo should be obvious in the push, not silently blank."""
    assert render("{sender} {nope}", {"sender": "10086"}) == "10086 {nope}"


def test_render_survives_braces_in_the_message():
    """str.format would raise here, and the code would never be delivered."""
    text = render(DEFAULT_TEMPLATE, {
        "card": "移动卡", "sender": "10086", "message": "code {0} and {oops}",
    })
    assert "code {0} and {oops}" in text


def test_empty_template_falls_back_to_the_default():
    assert render("", {"card": "A", "sender": "B", "message": "C"}) == "【A】B\nC"


def test_scrub_removes_urls():
    detail = scrub("ConnectError: https://api.telegram.org/bot42:SECRET/sendMessage")
    assert "SECRET" not in detail
    assert "<url>" in detail


# --------------------------------------------------------------------------
# rule matching
# --------------------------------------------------------------------------


def test_keyword_match_is_case_insensitive(db):
    channel = add_channel(db)
    add_rule(db, channel_id=channel, match="keyword", pattern="Verify")
    assert len(match_rules(db, sim_id=None, body="your verify code is 1234")) == 1
    assert match_rules(db, sim_id=None, body="nothing here") == []


def test_regex_match(db):
    channel = add_channel(db)
    add_rule(db, channel_id=channel, match="regex", pattern=r"\d{4,6}")
    assert len(match_rules(db, sim_id=None, body="code 123456")) == 1
    assert match_rules(db, sim_id=None, body="no digits") == []


def test_rule_scoped_to_another_card_does_not_fire(db):
    mine = db.upsert_sim(ICCID)
    theirs = db.upsert_sim(OTHER_ICCID)
    channel = add_channel(db)
    add_rule(db, channel_id=channel, sim_id=theirs)
    assert match_rules(db, sim_id=mine, body="anything") == []
    assert len(match_rules(db, sim_id=theirs, body="anything")) == 1


def test_disabled_rule_or_channel_never_fires(db):
    off_channel = add_channel(db, name="停用的渠道", enabled=False)
    add_rule(db, channel_id=off_channel)
    live_channel = add_channel(db, name="启用的渠道")
    add_rule(db, channel_id=live_channel, enabled=False)
    assert match_rules(db, sim_id=None, body="anything") == []


def test_invalid_regex_only_skips_its_own_rule(db, caplog):
    broken = add_channel(db, name="坏正则")
    add_rule(db, channel_id=broken, match="regex", pattern="[unclosed")
    good = add_channel(db, name="全部")
    add_rule(db, channel_id=good)

    with caplog.at_level(logging.WARNING):
        matched = match_rules(db, sim_id=None, body="验证码 123456")

    assert [channel["id"] for _, channel in matched] == [good]
    assert "invalid regex" in caplog.text


def test_keyword_rule_without_a_pattern_matches_nothing(db):
    """A half-filled form must not turn into a firehose."""
    channel = add_channel(db)
    add_rule(db, channel_id=channel, match="keyword", pattern="")
    assert match_rules(db, sim_id=None, body="anything at all") == []


def test_two_rules_on_one_channel_push_once(db):
    channel = add_channel(db)
    add_rule(db, channel_id=channel, template="低优先级", priority=0)
    add_rule(db, channel_id=channel, template="高优先级", priority=10)

    matched = match_rules(db, sim_id=None, body="验证码 123456")
    assert len(matched) == 1, "one channel, one push"
    assert matched[0][0]["template"] == "高优先级"


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------


async def test_one_sms_fans_out_to_every_matched_channel(db, settings):
    """M4's acceptance criterion: one SMS, several channels, complete logs."""
    bark = add_channel(db, name="Bark", type="bark", config={"url": "https://api.day.app/k"})
    wecom = add_channel(db, name="企业微信", type="wecom",
                        config={"webhook": "https://qyapi.weixin.qq.com/x"})
    add_rule(db, channel_id=bark)
    add_rule(db, channel_id=wecom)
    message_id = add_message(db)

    recorder = Recorder()
    notifier = make_notifier(db, settings, recorder)
    results = await notifier.deliver(message_id)

    assert {r["status"] for r in results} == {"ok"}
    assert len(recorder.requests) == 2
    logs = notify_logs(db)
    assert len(logs) == 2
    assert {log["status"] for log in logs} == {"ok"}
    assert {log["message_id"] for log in logs} == {message_id}


async def test_no_rules_means_no_push(db, settings):
    message_id = add_message(db)
    recorder = Recorder()
    notifier = make_notifier(db, settings, recorder)

    assert await notifier.deliver(message_id) == []
    assert recorder.requests == []
    assert notify_logs(db) == []


async def test_data_sms_is_never_forwarded_by_notification_rules(db, settings):
    channel = add_channel(db)
    add_rule(db, channel_id=channel, match="all")
    message_id = add_message(db, body="鼠S耸盘涌羹", is_binary=True)
    recorder = Recorder()
    notifier = make_notifier(db, settings, recorder)

    assert await notifier.deliver(message_id) == []
    assert recorder.requests == []
    assert notify_logs(db) == []


async def test_retry_then_success(db, settings):
    channel = add_channel(db)
    add_rule(db, channel_id=channel)
    message_id = add_message(db)

    recorder = Recorder(
        httpx.Response(502, text="bad gateway"),
        httpx.Response(500, text="boom"),
        httpx.Response(200, json={}),
    )
    notifier = make_notifier(db, settings, recorder, retries=2)
    results = await notifier.deliver(message_id)

    assert results[0]["status"] == "ok"
    assert results[0]["attempts"] == 3
    assert len(recorder.requests) == 3
    assert notify_logs(db)[0]["attempts"] == 3


async def test_retry_exhausted_records_the_providers_complaint(db, settings):
    channel = add_channel(db)
    add_rule(db, channel_id=channel)
    message_id = add_message(db)

    recorder = Recorder(*[
        httpx.Response(403, json={"msg": "webhook is disabled"}) for _ in range(3)
    ])
    notifier = make_notifier(db, settings, recorder, retries=2)
    results = await notifier.deliver(message_id)

    assert results[0]["status"] == "failed"
    assert results[0]["attempts"] == 3
    log = notify_logs(db)[0]
    assert log["status"] == "failed"
    assert "webhook is disabled" in log["detail"]
    assert "403" in log["detail"]


@pytest.mark.parametrize(
    "channel_type,config,response",
    [
        ("telegram", {"token": "42:ABC", "chat_id": "7"},
         httpx.Response(200, json={"ok": False, "description": "chat not found"})),
        ("wecom", {"webhook": "https://qyapi.weixin.qq.com/x"},
         httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook"})),
        ("dingtalk", {"webhook": "https://oapi.dingtalk.com/x"},
         httpx.Response(200, json={"errcode": 310000, "errmsg": "keywords not in content"})),
        ("feishu", {"webhook": "https://open.feishu.cn/x"},
         httpx.Response(200, json={"code": 19021, "msg": "sign match fail"})),
        ("bark", {"url": "https://api.day.app/k"},
         httpx.Response(200, json={"code": 400, "message": "device key invalid"})),
    ],
)
async def test_http_200_with_an_error_code_is_still_a_failure(
    db, settings, channel_type, config, response
):
    """Every one of these answers 200 and delivers nothing."""
    channel = add_channel(db, type=channel_type, config=config)
    add_rule(db, channel_id=channel)
    message_id = add_message(db)

    notifier = make_notifier(db, settings, Recorder(response), retries=0)
    results = await notifier.deliver(message_id)

    assert results[0]["status"] == "failed"
    assert notify_logs(db)[0]["status"] == "failed"


# --------------------------------------------------------------------------
# per-channel request shapes
# --------------------------------------------------------------------------


async def _send_one(db, settings, *, type, config, template="{message}") -> Recorder:
    # Each call is a clean slate: a leftover rule from the previous call would
    # add a second request and make requests[0] the wrong one.
    db.execute("DELETE FROM rules")
    db.execute("DELETE FROM channels")
    channel = add_channel(db, type=type, config=config)
    add_rule(db, channel_id=channel, template=template)
    message_id = add_message(db)
    recorder = Recorder()
    await make_notifier(db, settings, recorder, retries=0).deliver(message_id)
    return recorder


async def test_bark_payload(db, settings):
    recorder = await _send_one(
        db, settings, type="bark", config={"url": "https://api.day.app/key"}
    )
    request = recorder.requests[0]
    assert str(request.url) == "https://api.day.app/key"
    assert recorder.bodies[0]["body"] == "验证码 123456"
    assert recorder.bodies[0]["title"]


async def test_telegram_payload(db, settings):
    recorder = await _send_one(
        db, settings, type="telegram", config={"token": "42:ABC", "chat_id": "7"}
    )
    request = recorder.requests[0]
    assert request.url.path == "/bot42:ABC/sendMessage"
    assert recorder.bodies[0]["chat_id"] == "7"
    assert recorder.bodies[0]["text"] == "验证码 123456"


async def test_wecom_payload(db, settings):
    recorder = await _send_one(
        db, settings, type="wecom", config={"webhook": "https://qyapi.weixin.qq.com/x"}
    )
    assert recorder.bodies[0] == {
        "msgtype": "text", "text": {"content": "验证码 123456"},
    }


async def test_feishu_payload_without_a_secret_is_unsigned(db, settings):
    recorder = await _send_one(
        db, settings, type="feishu", config={"webhook": "https://open.feishu.cn/x"}
    )
    body = recorder.bodies[0]
    assert body["msg_type"] == "interactive"
    card = body["card"]
    assert card["config"] == {"wide_screen_mode": True, "enable_forward": False}
    assert card["header"]["title"]["content"] == "…5670 · 10086"
    assert card["header"]["template"] == "blue"
    assert card["elements"][0]["fields"] == [
        {
            "is_short": True,
            "text": {"tag": "plain_text", "content": "卡片\n…5670"},
        },
        {
            "is_short": True,
            "text": {"tag": "plain_text", "content": "发件人\n10086"},
        },
    ]
    assert card["elements"][2]["text"] == {
        "tag": "plain_text", "content": "验证码 123456",
    }
    assert card["elements"][-1]["tag"] == "note"
    assert "sign" not in body


async def test_feishu_signs_when_a_secret_is_configured(db, settings):
    recorder = await _send_one(
        db, settings, type="feishu",
        config={"webhook": "https://open.feishu.cn/x", "secret": "s3cret"},
    )
    body = recorder.bodies[0]
    assert body["sign"] and body["timestamp"]


def test_feishu_card_keeps_a_custom_template_and_sms_markdown_literal():
    context = {
        "card": "主卡", "sender": "95588", "message": "[验证码](https://example.test)",
        "timestamp": "2026-08-07 12:00:00", "device": "a", "iccid": "",
    }
    card = _feishu_card(Payload(
        text="提醒:[验证码](https://example.test)",
        title="银行短信",
        context=context,
    ))

    body = card["elements"][2]["text"]
    assert body == {
        "tag": "plain_text", "content": "提醒:[验证码](https://example.test)",
    }


async def test_feishu_system_notification_has_no_fake_sample_metadata(db, settings):
    add_channel(
        db, type="feishu", config={"webhook": "https://open.feishu.cn/x"}
    )
    recorder = Recorder(httpx.Response(200, json={"code": 0}))
    notifier = make_notifier(db, settings, recorder, retries=0)

    results = await notifier.notify_text("保号任务执行成功", title="保号任务")

    assert results[0]["status"] == "ok"
    card = recorder.bodies[0]["card"]
    encoded = json.dumps(card, ensure_ascii=False)
    assert "测试卡" not in encoded
    assert "10086" not in encoded
    assert card["header"]["title"]["content"] == "保号任务"
    assert card["elements"][0]["text"]["content"] == "保号任务执行成功"


async def test_dingtalk_signs_in_the_query_only_with_a_secret(db, settings):
    plain = await _send_one(
        db, settings, type="dingtalk", config={"webhook": "https://oapi.dingtalk.com/x"}
    )
    assert "sign" not in plain.requests[0].url.params

    signed = await _send_one(
        db, settings, type="dingtalk",
        config={"webhook": "https://oapi.dingtalk.com/x?access_token=t", "secret": "s"},
    )
    params = signed.requests[0].url.params
    assert params["sign"] and params["timestamp"]
    assert params["access_token"] == "t", "the original query must survive"


async def test_post_channel_sends_the_whole_context(db, settings):
    recorder = await _send_one(
        db, settings, type="post",
        config={"url": "https://sink.test/hook", "headers": {"X-Token": "abc"}},
    )
    body = recorder.bodies[0]
    assert body["sender"] == "10086"
    assert body["message"] == "验证码 123456"
    assert body["card"] and body["timestamp"]
    assert recorder.requests[0].headers["x-token"] == "abc"


async def test_get_channel_merges_into_the_existing_query(db, settings):
    recorder = await _send_one(
        db, settings, type="get", config={"url": "https://sink.test/send?key=abc"}
    )
    params = recorder.requests[0].url.params
    assert params["key"] == "abc"
    assert params["sender"] == "10086"
    assert params["text"] == "验证码 123456"


async def test_smtp_channel_sends_mail(db, settings, monkeypatch):
    sent: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host, self.port = host, port
            self.logged_in = None
            sent.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            self.logged_in = (user, password)

        def send_message(self, message):
            self.message = message

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)

    recorder = await _send_one(
        db, settings, type="smtp",
        config={"host": "smtp.test", "username": "me@test", "password": "pw",
                "to": "a@test, b@test"},
    )
    assert recorder.requests == [], "mail must not go out over HTTP"
    assert len(sent) == 1
    server = sent[0]
    assert (server.host, server.port) == ("smtp.test", 465)
    assert server.logged_in == ("me@test", "pw")
    assert server.message["To"] == "a@test, b@test"
    assert "验证码 123456" in server.message.get_content()


async def test_unknown_channel_type_fails_without_raising(db, settings):
    channel = add_channel(db, type="carrier-pigeon", config={})
    add_rule(db, channel_id=channel)
    message_id = add_message(db)

    notifier = make_notifier(db, settings, Recorder(), retries=0)
    results = await notifier.deliver(message_id)

    assert results[0]["status"] == "failed"
    assert "carrier-pigeon" in notify_logs(db)[0]["detail"]


async def test_missing_config_field_is_reported_not_raised(db, settings):
    channel = add_channel(db, type="bark", config={})
    add_rule(db, channel_id=channel)
    message_id = add_message(db)

    notifier = make_notifier(db, settings, Recorder(), retries=0)
    results = await notifier.deliver(message_id)

    assert results[0]["status"] == "failed"
    assert "'url'" in notify_logs(db)[0]["detail"]


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------


async def test_timestamp_renders_in_the_configured_zone(db, settings):
    """Stored UTC, shown in Asia/Shanghai — 18:00+08:00 must read as 18:00."""
    channel = add_channel(db)
    add_rule(db, channel_id=channel, template="{timestamp}")
    message_id = add_message(db)

    recorder = Recorder()
    await make_notifier(db, settings, recorder, retries=0).deliver(message_id)
    assert recorder.bodies[0]["text"] == "2026-08-02 18:00:00"


async def test_card_name_prefers_the_label(db, settings):
    channel = add_channel(db)
    add_rule(db, channel_id=channel, template="{card}")
    message_id = add_message(db)
    recorder = Recorder()

    # Unlabelled: the ICCID tail is the only handle the user has.
    await make_notifier(db, settings, recorder, retries=0).deliver(message_id)
    assert recorder.bodies[0]["text"] == "…5670"

    db.execute("UPDATE sims SET label = ? WHERE iccid = ?", ("移动保号卡", ICCID))
    await make_notifier(db, settings, recorder, retries=0).deliver(message_id)
    assert recorder.bodies[1]["text"] == "移动保号卡"


# --------------------------------------------------------------------------
# security baseline: SMS bodies never enter audit logs
# --------------------------------------------------------------------------


async def test_message_body_never_reaches_the_audit_log(db, settings, caplog):
    secret_body = "您的验证码是 998877,请勿告知他人"
    channel = add_channel(db)
    add_rule(db, channel_id=channel)
    message_id = add_message(db, body=secret_body)

    recorder = Recorder(
        httpx.Response(500, json={"msg": "internal error"}),
        httpx.Response(200, json={}),
    )
    with caplog.at_level(logging.DEBUG):
        await make_notifier(db, settings, recorder, retries=1).deliver(message_id)

    for log in notify_logs(db):
        assert "998877" not in log["detail"]
        assert secret_body not in log["detail"]
    assert "998877" not in caplog.text


async def test_a_bot_token_in_an_error_is_scrubbed(db, settings):
    channel = add_channel(db, type="telegram", config={"token": "42:SECRET", "chat_id": "7"})
    add_rule(db, channel_id=channel)
    message_id = add_message(db)

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to {request.url}")

    notifier = make_notifier(db, settings, explode, retries=0)
    results = await notifier.deliver(message_id)

    assert results[0]["status"] == "failed"
    detail = notify_logs(db)[0]["detail"]
    assert "SECRET" not in detail
    assert "<url>" in detail


# --------------------------------------------------------------------------
# dispatch must not block ingest
# --------------------------------------------------------------------------


async def test_on_message_returns_before_the_push_completes(db, settings):
    """The gateway acks after this returns; a slow provider must not hold it."""
    channel = add_channel(db)
    add_rule(db, channel_id=channel)
    message_id = add_message(db)

    released = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        await released.wait()
        return httpx.Response(200, json={})

    notifier = make_notifier(db, settings, slow, retries=0)

    await notifier.on_message(message_id, {})
    assert notify_logs(db) == [], "on_message must not wait for the provider"

    released.set()
    await notifier.drain()
    assert [log["status"] for log in notify_logs(db)] == ["ok"]


async def test_a_failing_delivery_does_not_escape_the_task(db, settings):
    channel = add_channel(db)
    add_rule(db, channel_id=channel)

    notifier = make_notifier(db, settings, Recorder(), retries=0)
    await notifier.on_message(999_999, {"body": "no such message row"})
    await notifier.drain()  # must not raise


async def test_notify_text_reaches_every_enabled_channel(db, settings):
    """The hook M5 will call for keep-alive task results."""
    add_channel(db, name="on")
    add_channel(db, name="off", enabled=False)

    recorder = Recorder()
    notifier = make_notifier(db, settings, recorder, retries=0)
    results = await notifier.notify_text("保号任务执行成功")

    assert len(results) == 1
    assert recorder.bodies[0]["text"] == "保号任务执行成功"


# --------------------------------------------------------------------------
# API surface
# --------------------------------------------------------------------------


@pytest.fixture
def admin(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/setup", json={"password": "hunter2hunter"})
        yield client


def install(client, recorder) -> Notifier:
    notifier = client.app.state.hub.notifier
    notifier.client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    notifier.backoff = (0.0, 0.0)
    return notifier


def test_channel_test_button_reports_success(admin):
    recorder = Recorder()
    install(admin, recorder)
    channel = admin.post("/api/channels", json={
        "name": "Bark", "type": "bark", "config": {"url": "https://api.day.app/k"},
    }).json()

    response = admin.post(f"/api/channels/{channel['id']}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert len(recorder.requests) == 1

    logs = admin.get("/api/notify-logs").json()["items"]
    assert logs[0]["status"] == "ok"
    assert logs[0]["message_id"] is None, "a test send belongs to no message"


def test_channel_test_button_passes_the_provider_error_through(admin):
    install(admin, Recorder(httpx.Response(200, json={"code": 400, "message": "bad key"})))
    channel = admin.post("/api/channels", json={
        "name": "Bark", "type": "bark", "config": {"url": "https://api.day.app/k"},
    }).json()

    response = admin.post(f"/api/channels/{channel['id']}/test")
    assert response.status_code == 502
    assert "bad key" in response.json()["detail"]


def test_channel_test_does_not_retry(admin):
    """An admin waiting on a button wants the answer now."""
    recorder = Recorder(*[httpx.Response(500, text="down") for _ in range(4)])
    install(admin, recorder)
    channel = admin.post("/api/channels", json={
        "name": "Bark", "type": "bark", "config": {"url": "https://api.day.app/k"},
    }).json()

    admin.post(f"/api/channels/{channel['id']}/test")
    assert len(recorder.requests) == 1


def test_testing_an_unknown_channel_is_404(admin):
    assert admin.post("/api/channels/999/test").status_code == 404


def test_rule_preview_uses_the_same_rendering_as_delivery(admin, settings):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    db = admin.app.state.hub.db
    sim_id = db.upsert_sim(ICCID)
    db.execute("UPDATE sims SET label = ? WHERE id = ?", ("移动主卡", sim_id))
    channel = admin.post("/api/channels", json={
        "name": "Bark",
        "type": "bark",
        "config": {
            "url": "https://api.day.app/k",
            "title": "告警 {card}",
        },
    }).json()
    admin.post("/api/rules", json={
        "name": "验证码",
        "channel_id": channel["id"],
        "match": "keyword",
        "pattern": "验证码",
        "template": "{timestamp}|{sender}|{message}",
    })

    response = admin.post("/api/rules/preview", json={
        "sim_id": sim_id,
        "peer": "10086",
        "body": "验证码 123456",
    })
    assert response.status_code == 200
    hit = response.json()[0]
    assert hit["title"] == "告警 移动主卡"
    timestamp, sender, message = hit["text"].split("|")
    assert sender == "10086"
    assert message == "验证码 123456"
    datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    assert timestamp[:10] == datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")


def test_channel_can_be_updated_and_disabled(admin):
    channel = admin.post("/api/channels", json={
        "name": "Bark", "type": "bark", "config": {"url": "https://api.day.app/k"},
    }).json()

    updated = admin.put(f"/api/channels/{channel['id']}", json={
        "name": "Bark 备用", "type": "bark",
        "config": {"url": "https://api.day.app/other"}, "enabled": False,
    })
    assert updated.status_code == 200
    assert updated.json()["enabled"] == 0
    assert updated.json()["name"] == "Bark 备用"
    assert json.loads(updated.json()["config"])["url"].endswith("other")


def test_rule_can_be_updated(admin):
    channel = admin.post("/api/channels", json={
        "name": "Bark", "type": "bark", "config": {"url": "https://api.day.app/k"},
    }).json()
    rule = admin.post("/api/rules", json={"channel_id": channel["id"]}).json()

    updated = admin.put(f"/api/rules/{rule['id']}", json={
        "channel_id": channel["id"], "match": "keyword", "pattern": "验证码",
        "template": "{sender}: {message}", "priority": 5, "enabled": False,
    })
    assert updated.status_code == 200
    assert updated.json()["template"] == "{sender}: {message}"
    assert updated.json()["enabled"] == 0


def test_updating_something_that_does_not_exist_is_404(admin):
    assert admin.put("/api/channels/404", json={"name": "x", "type": "bark"}).status_code == 404
    assert admin.put("/api/rules/404", json={"channel_id": 1}).status_code == 404


def test_new_routes_reject_anonymous(client_without_setup):
    client = client_without_setup
    assert client.post("/api/channels/1/test").status_code == 401
    assert client.put("/api/channels/1", json={"name": "x", "type": "bark"}).status_code == 401
    assert client.put("/api/rules/1", json={"channel_id": 1}).status_code == 401


@pytest.fixture
def client_without_setup(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


# --------------------------------------------------------------------------
# end to end over the agent socket
# --------------------------------------------------------------------------


HELLO = {
    "type": "hello",
    "agent_id": "home-arch",
    "version": "0.1.0",
    "protocol_version": 1,
    "last_seq": 0,
    "devices": [
        {"name": "a", "label": "移动卡", "port": "/dev/air780e-a", "online": True,
         "iccid": ICCID, "operator": "CHINA MOBILE", "registered": True},
    ],
}


def _greet(ws) -> None:
    """Say hello and read the task push every connection is answered with."""
    ws.send_json(HELLO)
    assert ws.receive_json()["type"] == "sync_tasks"


def test_an_sms_over_the_socket_is_pushed(admin):
    recorder = Recorder()
    install(admin, recorder)
    channel = admin.post("/api/channels", json={
        "name": "Bark", "type": "bark", "config": {"url": "https://api.day.app/k"},
    }).json()
    admin.post("/api/rules", json={
        "channel_id": channel["id"], "match": "keyword", "pattern": "验证码",
    })

    with admin.websocket_connect(
        "/ws", headers={"Authorization": "Bearer test-token"}
    ) as ws:
        _greet(ws)
        ws.send_json({
            "type": "sms_in", "seq": 1, "device": "a", "iccid": ICCID,
            "peer": "10086", "body": "验证码 123456",
            "ts": "2026-08-02T18:00:00+08:00", "segments": 1,
        })
        assert ws.receive_json() == {"type": "ack", "seq": 1}

    _wait_for(lambda: recorder.requests)

    assert len(recorder.requests) == 1, "the SMS should have been pushed"
    body = json.loads(recorder.requests[0].content)
    assert "验证码 123456" in body["body"]
    assert "移动卡" in body["body"], "the card's label names the source"

    logs = admin.get("/api/notify-logs").json()["items"]
    assert logs[0]["status"] == "ok"
    assert logs[0]["channel_name"] == "Bark"


# --------------------------------------------------------------------------
# keep-alive receipts (M5)
# --------------------------------------------------------------------------


def _wait_for(probe, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.02)
    return probe()


def make_task(admin, **overrides) -> dict:
    body = {
        "name": "移动卡保号", "device": "a", "agent_id": "home-arch",
        "target_number": "10086", "content": "1",
        "schedule_type": "interval", "schedule_expr": "25",
        **overrides,
    }
    response = admin.post("/api/tasks", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def send_receipt(admin, task_id: int, **overrides) -> None:
    frame = {
        "type": "task_result", "seq": 1, "task_id": task_id, "device": "a",
        "ts": "2026-08-27T03:14:00+08:00", "status": "ok", "attempts": 1,
        "detail": 'sent to 10086: "1 f3a9" (1 segment(s))', "error": None,
        "next_run_at": "2026-09-21T03:22:00+08:00",
        **overrides,
    }
    with admin.websocket_connect(
        "/ws", headers={"Authorization": "Bearer test-token"}
    ) as ws:
        _greet(ws)
        ws.send_json(frame)
        assert ws.receive_json() == {"type": "ack", "seq": frame["seq"]}


def test_a_task_receipt_is_logged_and_dates_the_next_run(admin):
    install(admin, Recorder())
    task = make_task(admin, notify_on_result=False)
    send_receipt(admin, task["id"])

    logs = admin.get("/api/task-logs").json()["items"]
    assert logs[0]["status"] == "ok"
    assert logs[0]["task_name"] == "移动卡保号"

    stored = admin.get("/api/tasks").json()[0]
    assert stored["last_run_at"].startswith("2026-08-27")
    assert stored["next_run_at"].startswith("2026-09-21")


def test_a_task_receipt_is_pushed_when_the_task_asks_for_it(admin):
    recorder = Recorder()
    install(admin, recorder)
    admin.post("/api/channels", json={
        "name": "Bark", "type": "bark", "config": {"url": "https://api.day.app/k"},
    })
    task = make_task(admin, notify_on_result=True)

    send_receipt(admin, task["id"])
    _wait_for(lambda: recorder.requests)

    assert len(recorder.requests) == 1
    text = json.loads(recorder.requests[0].content)["body"]
    assert "保号" in text and "执行成功" in text
    assert "移动卡保号" in text
    assert "下次" in text


def test_a_failed_task_receipt_carries_the_reason(admin):
    recorder = Recorder()
    install(admin, recorder)
    admin.post("/api/channels", json={
        "name": "Bark", "type": "bark", "config": {"url": "https://api.day.app/k"},
    })
    task = make_task(admin)

    send_receipt(
        admin, task["id"], status="failed", attempts=3, detail="",
        error="device a is offline: not connected",
    )
    _wait_for(lambda: recorder.requests)

    text = json.loads(recorder.requests[0].content)["body"]
    assert "执行失败" in text
    assert "offline" in text
    assert "尝试 3 次" in text


def test_no_push_when_the_task_opted_out(admin):
    recorder = Recorder()
    install(admin, recorder)
    admin.post("/api/channels", json={
        "name": "Bark", "type": "bark", "config": {"url": "https://api.day.app/k"},
    })
    task = make_task(admin, notify_on_result=False)

    send_receipt(admin, task["id"])
    time.sleep(0.3)
    assert recorder.requests == []


def test_a_skipped_task_did_not_run(admin):
    """A skipped attempt must not restart the interval clock."""
    install(admin, Recorder())
    task = make_task(admin, notify_on_result=False)

    send_receipt(
        admin, task["id"], status="skipped",
        error="no device named 'a' on this agent",
    )

    stored = admin.get("/api/tasks").json()[0]
    assert stored["last_run_at"] is None
    assert stored["next_run_at"].startswith("2026-09-21"), "still re-planned"
