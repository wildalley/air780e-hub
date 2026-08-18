"""AT framing, error mapping and URC dispatch."""

from __future__ import annotations

import asyncio

import pytest

from air780e_agent.at import ATCommandError, ATTimeout, ATUrc, CmeError, CmsError


async def test_simple_command(rig):
    response = await rig.client.execute("AT")
    assert response.final == "OK"
    assert response.lines == []


async def test_informational_response(rig):
    response = await rig.client.execute("ATI")
    assert response.lines == [rig.mock.model]


async def test_response_accessors(rig):
    rig.mock.rssi = 19
    response = await rig.client.execute("AT+CSQ")
    assert response.first("+CSQ:") == "19,99"
    assert response.first("+NOPE:") is None
    assert response.all("+CSQ:") == ["19,99"]


async def test_unknown_command_raises_cme(rig):
    with pytest.raises(CmeError) as excinfo:
        await rig.client.execute("AT+NOSUCHTHING")
    assert excinfo.value.code == 4
    assert "not supported" in str(excinfo.value)


async def test_bare_error_when_cmee_disabled(rig):
    rig.mock._cmee = 0
    with pytest.raises(ATCommandError):
        await rig.client.execute("AT+NOSUCHTHING")


async def test_cms_error_carries_code(rig):
    with pytest.raises(CmsError) as excinfo:
        await rig.client.execute("AT+CMGR=999")
    assert excinfo.value.code == 321


async def test_timeout_when_modem_says_nothing(rig):
    rig.mock.unsupported.add("AT+SILENT")

    # Swap the handler for one that never answers at all.
    def swallow(line: str) -> None:
        pass

    rig.mock._dispatch = swallow  # type: ignore[method-assign]
    with pytest.raises(ATTimeout):
        await rig.client.execute("AT+SILENT", timeout=0.05)


async def test_commands_are_serialized(rig):
    results = await asyncio.gather(
        rig.client.execute("ATI"),
        rig.client.execute("AT+CSQ"),
        rig.client.execute("AT+CPIN?"),
    )
    assert results[0].lines == [rig.mock.model]
    assert results[1].first("+CSQ:") is not None
    assert results[2].lines == ["+CPIN: READY"]


# --------------------------------------------------------------------------
# URC handling
# --------------------------------------------------------------------------


async def test_urc_dispatch(rig):
    seen: list[ATUrc] = []
    rig.client.register_urc("+CMTI", seen.append)

    rig.mock.deliver("10086", "hello")
    await asyncio.sleep(0.05)

    assert len(seen) == 1
    assert seen[0].name == "+CMTI"
    assert seen[0].params == '"SM",1'


async def test_urc_arriving_during_a_command_does_not_corrupt_it(rig):
    seen: list[ATUrc] = []
    rig.client.register_urc("+CMTI", seen.append)

    async def interrupt():
        await asyncio.sleep(0)
        rig.mock.deliver("10086", "interrupting")

    response, _ = await asyncio.gather(rig.client.execute("ATI"), interrupt())

    # The URC must not have leaked into the command's informational lines.
    assert response.lines == [rig.mock.model]
    await asyncio.sleep(0.05)
    assert len(seen) == 1


async def test_unregistered_prefix_is_treated_as_response_data(rig):
    # +CMGL looks like a URC but is a response; it must reach the caller.
    rig.mock.deliver("10086", "in the inbox")
    await asyncio.sleep(0.05)
    response = await rig.client.execute("AT+CMGL=4")
    assert any(line.startswith("+CMGL:") for line in response.lines)


async def test_multiline_urc_captures_payload(rig):
    seen: list[ATUrc] = []
    rig.client.register_urc("+XPAYLOAD", seen.append, payload_lines=1)

    rig.mock._urc("+XPAYLOAD: ,23")
    rig.mock._write("0891683108200105F0\r\n")
    await asyncio.sleep(0.05)

    assert len(seen) == 1
    assert seen[0].payload == ["0891683108200105F0"]


async def test_failing_urc_handler_does_not_break_the_client(rig):
    def explode(urc: ATUrc) -> None:
        raise RuntimeError("handler is broken")

    rig.client.register_urc("+CMTI", explode)
    rig.mock.deliver("10086", "boom")
    await asyncio.sleep(0.05)

    # The client must still be usable.
    assert (await rig.client.execute("AT")).final == "OK"


async def test_query_response_beats_a_urc_of_the_same_name(rig):
    """+CEREG is both a URC and the answer to AT+CEREG?.

    Registering the URC must not stop the query from getting its reply, or
    every registration read comes back empty.
    """
    seen: list[ATUrc] = []
    rig.client.register_urc("+CEREG", seen.append)

    response = await rig.client.execute("AT+CEREG?")
    assert response.lines == ["+CEREG: 0,1"]
    assert seen == [], "the response was misrouted to the URC handler"


async def test_urc_of_the_same_name_still_fires_when_idle(rig):
    seen: list[ATUrc] = []
    rig.client.register_urc("+CEREG", seen.append)

    rig.mock._urc("+CEREG: 0,2")
    await asyncio.sleep(0.05)
    assert [u.params for u in seen] == ["0,2"]


async def test_urc_prefixes_do_not_shadow_each_other(rig):
    """"+CMT" must not swallow "+CMTI" just because it is a prefix of it."""
    cmt: list[ATUrc] = []
    cmti: list[ATUrc] = []
    rig.client.register_urc("+CMT", cmt.append, payload_lines=1)
    rig.client.register_urc("+CMTI", cmti.append)

    rig.mock.deliver("10086", "which handler?")
    await asyncio.sleep(0.05)

    assert len(cmti) == 1
    assert cmt == []


# --------------------------------------------------------------------------
# the two-step send prompt
# --------------------------------------------------------------------------


async def test_prompt_flow(rig):
    from air780e_agent.pdu import encode_submit

    part = encode_submit("10086", "CXHF")[0]
    response = await rig.client.execute(
        f"AT+CMGS={part.tpdu_len}",
        payload=part.pdu_hex + "\x1a",
        expect_prompt=True,
    )
    assert response.first("+CMGS:") == "0"
    assert len(rig.mock.sent) == 1
    assert rig.mock.sent[0].text == "CXHF"


async def test_prompt_timeout_is_reported_clearly(rig):
    rig.mock.unsupported.add("AT+CMGS")
    with pytest.raises(ATTimeout) as excinfo:
        await rig.client.execute(
            "AT+CMGS=10", payload="x\x1a", expect_prompt=True, prompt_timeout=0.05
        )
    assert "prompt" in str(excinfo.value)


async def test_mock_rejects_length_mismatch(rig):
    from air780e_agent.pdu import encode_submit

    part = encode_submit("10086", "CXHF")[0]
    with pytest.raises(CmsError):
        await rig.client.execute(
            f"AT+CMGS={part.tpdu_len + 5}",
            payload=part.pdu_hex + "\x1a",
            expect_prompt=True,
        )


# --------------------------------------------------------------------------
# Text-mode +CME/+CMS errors (Air780E V1011 ignores CMEE=1)
# --------------------------------------------------------------------------


async def test_text_cms_error_recovers_numeric_code(rig):
    """Firmware that answers CMEE=1 with text must still yield `.code`.

    Measured on AirM2M_780EPV_V1011: `+CMS ERROR: invalid memory index`
    arrives even after `AT+CMEE=1`.  Without the reverse lookup this became a
    bare ATCommandError and every distinct cause looked identical.
    """
    rig.mock.force_text_errors = True
    with pytest.raises(CmsError) as excinfo:
        await rig.client.execute("AT+CMGR=999")
    assert excinfo.value.code == 321


async def test_text_cme_error_recovers_numeric_code(rig):
    rig.mock.force_text_errors = True
    with pytest.raises(CmeError) as excinfo:
        await rig.client.execute("AT+NOSUCHTHING")
    assert excinfo.value.code == 4


async def test_unknown_error_wording_stays_untyped(rig):
    """Wording the table does not know must not be mapped to a guess."""
    from air780e_agent.at import code_for_error_text

    assert code_for_error_text("CMS", "some vendor specific failure") is None
    assert code_for_error_text("CMS", "unknown error") == 500
    # Punctuation, case and internal whitespace must not defeat the lookup.
    assert code_for_error_text("CMS", "Unknown  Error.") == 500
    assert code_for_error_text("CME", "operation not supported") == 4
    # The two families are separate tables: 4 is CME-only wording.
    assert code_for_error_text("CMS", "operation not supported") is None


async def test_text_error_with_unknown_wording_raises_plain(rig, monkeypatch):
    """An unrecognized text error stays an ATCommandError, verbatim."""
    import air780e_agent.mock as mock_module

    monkeypatch.setitem(mock_module.CMS_ERRORS, 321, "vendor gibberish")
    rig.mock.force_text_errors = True
    with pytest.raises(ATCommandError) as excinfo:
        await rig.client.execute("AT+CMGR=999")
    assert "vendor gibberish" in str(excinfo.value)
    assert not isinstance(excinfo.value, CmsError)
