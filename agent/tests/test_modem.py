"""End-to-end driver behaviour against the mock modem.

The tests that matter most here are the storage ones: a slot that is not
freed is a message that will never arrive.
"""

from __future__ import annotations

import asyncio

import pytest

from air780e_agent.modem import Air780E, Signal

# --------------------------------------------------------------------------
# setup and inspection
# --------------------------------------------------------------------------


async def test_initialize_reads_identity(modem, rig):
    assert modem.info.model == rig.mock.model
    assert modem.info.manufacturer == rig.mock.manufacturer
    assert modem.info.hardware_model == rig.mock.hardware_model
    assert modem.info.firmware == rig.mock.firmware
    assert modem.info.imei == rig.mock.imei
    assert modem.info.iccid == rig.mock.iccid
    assert modem.info.smsc == rig.mock.smsc
    assert modem.info.operator == rig.mock.operator
    assert modem.info.registered is True
    assert modem.info.eps_registered is True
    assert modem.info.cs_registered is True
    assert modem.info.ims_registered is False


async def test_initialize_selects_pdu_mode(modem, rig):
    assert rig.mock._cmgf == 0, "text mode mangles non-ASCII content"
    assert rig.mock._cmee == 1, "numeric +CMS/+CME codes are required for diagnosis"
    assert rig.mock._echo is False
    assert rig.mock._cnmi.upper() == "AT+CNMI=2,1,0,1,0"
    assert "AT+CIREG=1" in rig.mock.commands


async def test_initialize_tolerates_firmware_without_ims_status(rig):
    rig.mock.unsupported.add("AT+CIREG")
    rig.modem = Air780E(rig.client)

    info = await rig.modem.initialize()

    assert info.registered is True
    assert info.ims_registered is None


async def test_read_signal(modem, rig):
    rig.mock.rssi = 24
    signal = await modem.read_signal()
    assert signal.rssi == 24
    assert signal.dbm == -65
    assert signal.bars == 5
    assert signal.rsrp == 55


async def test_signal_unknown_is_not_a_lie():
    signal = Signal(rssi=99)
    assert signal.dbm is None
    assert signal.bars == 0


async def test_storage_usage(modem, rig):
    rig.mock.fill_storage(3)
    used, capacity = await modem.storage_usage()
    assert used == 3
    assert capacity == rig.mock.capacity


# --------------------------------------------------------------------------
# receiving
# --------------------------------------------------------------------------


async def test_incoming_message_surfaces(modem, rig):
    rig.mock.deliver("10086", "your code is 123456")
    await rig.wait_for_sms()

    sms = rig.received[0]
    assert sms.address == "10086"
    assert sms.text == "your code is 123456"
    assert sms.timestamp is not None


async def test_incoming_chinese_message(modem, rig):
    text = "【测试】验证码 987654,5 分钟内有效,请勿泄露。"
    rig.mock.deliver("106900", text)
    await rig.wait_for_sms()
    assert rig.received[0].text == text


async def test_incoming_long_message_is_reassembled(modem, rig):
    text = "".join(chr(ord("a") + i % 26) for i in range(400))
    rig.mock.deliver("10086", text)
    await rig.wait_for_sms()

    assert len(rig.received) == 1, "segments must arrive as one message"
    assert rig.received[0].text == text


async def test_incoming_long_chinese_is_reassembled(modem, rig):
    text = "".join("中文长短信测试内容" [i % 9] for i in range(150))
    rig.mock.deliver("10086", text)
    await rig.wait_for_sms()
    assert rig.received[0].text == text


async def test_a_short_pdu_is_re_read_not_believed(modem, rig, caplog):
    """Real hardware returned a PDU shorter than its own header advertised;
    the body came out truncated with nothing anywhere saying so."""
    rig.mock.truncate_reads = 1
    rig.mock.deliver("10086", "your code is 123456")
    await rig.wait_for_sms()

    assert rig.received[0].text == "your code is 123456", "the re-read must win"
    assert any("re-reading" in r.message for r in caplog.records), \
        "a silent recovery hides a modem that is misbehaving"


async def test_a_pdu_that_stays_short_is_reported_loudly(modem, rig, caplog):
    rig.mock.truncate_reads = 99  # every read comes back short
    rig.mock.deliver("10086", "your code is 123456")
    await rig.wait_for_sms()

    assert rig.received[0].text != "your code is 123456", "premise: it is short"
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("truncated" in r.message for r in errors), \
        "losing part of a message must not be silent"


async def test_slot_is_freed_after_reading(modem, rig):
    rig.mock.deliver("10086", "transient")
    await rig.wait_for_sms()
    await asyncio.sleep(0.05)
    assert rig.mock.stored_count == 0, "storage must be released or it fills up"


async def test_every_segment_slot_is_freed(modem, rig):
    rig.mock.deliver("10086", "x" * 400)
    await rig.wait_for_sms()
    await asyncio.sleep(0.05)
    assert rig.mock.stored_count == 0


async def test_delete_can_be_disabled(rig):
    rig.modem = Air780E(rig.client, on_sms=rig.on_sms, delete_after_read=False)
    await rig.modem.initialize()

    rig.mock.deliver("10086", "keep me")
    await rig.wait_for_sms()
    await asyncio.sleep(0.05)
    assert rig.mock.stored_count == 1


async def test_many_messages_do_not_exhaust_storage(modem, rig):
    # Three times the capacity: only correct slot recycling gets us through.
    # Delivered in batches, because the modem cannot store faster than we drain.
    batch = 10
    total = rig.mock.capacity * 3
    for start in range(0, total, batch):
        for i in range(start, start + batch):
            assert rig.mock.deliver("10086", f"message {i}"), f"dropped at {i}"
        await rig.wait_for_sms(start + batch, timeout=10.0)
    assert len(rig.received) == total
    assert rig.mock.stored_count == 0


async def test_full_storage_drops_messages(rig):
    """Documents the failure mode the delete-after-read policy exists to avoid."""
    rig.mock.capacity = 3
    rig.mock.fill_storage(3)
    assert rig.mock.deliver("10086", "this one is lost") is False
    assert rig.mock.stored_count == 3


# --------------------------------------------------------------------------
# startup recovery
# --------------------------------------------------------------------------


async def test_drain_inbox_recovers_offline_messages(rig):
    # Messages that piled up while the agent was not running.
    rig.mock.deliver("10086", "first")
    rig.mock.deliver("10010", "second")

    rig.modem = Air780E(rig.client, on_sms=rig.on_sms)
    await rig.modem.initialize()
    recovered = await rig.modem.drain_inbox()

    assert {sms.text for sms in recovered} == {"first", "second"}
    assert rig.mock.stored_count == 0


async def test_drain_inbox_reassembles_partial_multipart(rig):
    text = "y" * 400
    rig.mock.deliver("10086", text)

    rig.modem = Air780E(rig.client, on_sms=rig.on_sms)
    await rig.modem.initialize()
    recovered = await rig.modem.drain_inbox()

    assert len(recovered) == 1
    assert recovered[0].text == text


async def test_drain_inbox_discards_undecodable_pdus(rig):
    from air780e_agent.mock import StoredMessage

    rig.mock._messages[99] = StoredMessage(99, 0, "00DEADBEEF")
    rig.mock.deliver("10086", "good one")

    rig.modem = Air780E(rig.client, on_sms=rig.on_sms)
    await rig.modem.initialize()
    recovered = await rig.modem.drain_inbox()

    assert [sms.text for sms in recovered] == ["good one"]
    # The bad PDU must be deleted too, or it blocks the slot forever.
    assert rig.mock.stored_count == 0


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


async def test_send_short_message(modem, rig):
    refs = await modem.send_sms("10086", "CXHF")
    assert refs == [0]
    assert len(rig.mock.sent) == 1
    assert rig.mock.sent[0].address == "10086"
    assert rig.mock.sent[0].text == "CXHF"


async def test_send_chinese_message(modem, rig):
    await modem.send_sms("10086", "保号短信 " + "内容")
    assert rig.mock.sent[0].text == "保号短信 内容"
    assert rig.mock.sent[0].alphabet == "ucs2"


async def test_send_long_message_splits(modem, rig):
    text = "z" * 400
    refs = await modem.send_sms("10086", text)
    assert len(refs) == 3
    assert len(rig.mock.sent) == 3
    assert "".join(part.text for part in rig.mock.sent) == text


async def test_delivery_report_surfaces_from_cds(modem, rig):
    refs = await modem.send_sms("10086", "CXHF")
    rig.mock.report_delivery(refs[0], "10086", status=0)

    async with asyncio.timeout(1.0):
        while not rig.deliveries:
            await asyncio.sleep(0.01)
    report = rig.deliveries[0]
    assert report.message_reference == refs[0]
    assert report.recipient == "10086"
    assert report.state == "delivered"


async def test_send_failure_propagates(modem, rig):
    from air780e_agent.at import CmsError

    rig.mock.fail_next_send = True
    with pytest.raises(CmsError):
        await modem.send_sms("10086", "will fail")
    assert rig.mock.sent == []


async def test_ping(modem, rig):
    assert await modem.ping("www.baidu.com") is True
    assert rig.mock.pings == ["www.baidu.com"]


async def test_ping_failure_is_reported_not_raised(modem, rig):
    rig.mock.unsupported.add("AT+CIPPING")
    assert await modem.ping() is False


# --------------------------------------------------------------------------
# sending and receiving at the same time
# --------------------------------------------------------------------------


async def test_incoming_message_during_send(modem, rig):
    async def interrupt():
        await asyncio.sleep(0)
        rig.mock.deliver("10086", "arrived mid-send")

    refs, _ = await asyncio.gather(
        modem.send_sms("10010", "outgoing"), interrupt()
    )
    await rig.wait_for_sms()

    assert refs == [0]
    assert rig.mock.sent[0].text == "outgoing"
    assert rig.received[0].text == "arrived mid-send"


async def test_cgreg_alias_survives_the_urc_router(rig):
    """The +CGREG answer must reach the response, not the URC handlers.

    `_handle_line` tries the in-flight command's expected prefix *before* the
    URC router, so an alias the router claimed would be dispatched as a
    registration change and never land in `response.lines`.  This exercises the
    real ATClient rather than a fake, which is the only way that ordering shows.
    """
    rig.mock.cereg_answers_as_cgreg = True
    modem = Air780E(rig.client)
    rig.modem = modem

    response = await rig.client.execute("AT+CEREG?")
    assert response.first("+CGREG:") == "0,1"

    assert await modem.read_registration() is True
    assert modem.info.eps_registered is True
