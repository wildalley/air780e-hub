"""Registration detection: the CS/EPS fallback and the URC parsing.

These exercise the driver against a hand-built fake client rather than the
full mock, because what matters here is the exact ``+CEREG``/``+CREG`` text
seen on the wire — including a 2G fallback where the two domains disagree, and
a mode-2 URC that carries location fields the old parser tripped over.
"""

from __future__ import annotations

import pytest

from air780e_agent.at import ATError, ATResponse, ATUrc
from air780e_agent.modem import Air780E, parse_current_operator, parse_operator_scan


class FakeClient:
    """Answers ``execute`` from a per-command script; records URC handlers.

    ``responses`` maps a command to either an :class:`ATResponse`, an
    ``ATError`` to raise, or a list consumed one call at a time (so a command
    can answer differently on successive polls, as during recovery).
    """

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.urc_handlers: dict[str, object] = {}

    def register_urc(self, prefix, handler, *, payload_lines: int = 0) -> None:
        self.urc_handlers[prefix.upper()] = handler

    async def execute(self, command: str, *, timeout=None, **kwargs) -> ATResponse:
        self.calls.append(command)
        result = self.responses.get(command)
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, ATError):
            raise result
        if isinstance(result, ATResponse):
            return result
        return ATResponse(command, [], "OK")


def _reg_response(command: str, prefix: str, stat: int) -> ATResponse:
    # Query form is "<n>,<stat>": +CEREG: 0,1
    return ATResponse(command, [f"{prefix}: 0,{stat}"], "OK")


def _modem(responses: dict[str, object]) -> Air780E:
    return Air780E(FakeClient(responses))


def test_current_operator_parser_handles_quoted_name_and_act():
    assert parse_current_operator('+COPS: 0,0,"CHINA MOBILE",7') == {
        "mode": 0,
        "format": 0,
        "operator": "CHINA MOBILE",
        "numeric": "",
        "access_technology": 7,
    }


def test_operator_scan_parser_handles_standard_groups_and_ignores_ranges():
    value = (
        '+COPS: (1,"CHINA MOBILE","CMCC","46000",7),'
        '(2,"Example, roaming","EX","23420",0),(0-4),(0-2)'
    )
    assert parse_operator_scan(value) == [
        {
            "status": 1,
            "long_name": "CHINA MOBILE",
            "short_name": "CMCC",
            "numeric": "46000",
            "access_technology": 7,
        },
        {
            "status": 2,
            "long_name": "Example, roaming",
            "short_name": "EX",
            "numeric": "23420",
            "access_technology": 0,
        },
    ]


# --------------------------------------------------------------------------
# radio state
# --------------------------------------------------------------------------


async def test_read_radio_enabled_understands_full_and_minimum_functionality():
    enabled = _modem({
        "AT+CFUN?": ATResponse("AT+CFUN?", ["+CFUN: 1"], "OK"),
    })
    disabled = _modem({
        "AT+CFUN?": ATResponse("AT+CFUN?", ["+CFUN: 0"], "OK"),
    })

    assert await enabled.read_radio_enabled() is True
    assert await disabled.read_radio_enabled() is False


async def test_unknown_radio_response_is_not_guessed():
    modem = _modem({"AT+CFUN?": ATResponse("AT+CFUN?", [], "OK")})
    assert await modem.read_radio_enabled() is None


async def test_set_radio_off_updates_driver_state_without_registration_poll():
    modem = _modem({"AT+CFUN=0": ATResponse("AT+CFUN=0", [], "OK")})
    modem.info.registered = True

    assert await modem.set_radio_enabled(False) == (False, False)
    assert modem.info.radio_enabled is False
    assert "AT+CEREG?" not in modem.client.calls


async def test_set_radio_on_reports_the_immediate_registration_state():
    modem = _modem({
        "AT+CFUN=1": ATResponse("AT+CFUN=1", [], "OK"),
        "AT+CEREG?": _reg_response("AT+CEREG?", "+CEREG", 1),
    })

    assert await modem.set_radio_enabled(True) == (True, True)
    assert modem.info.radio_enabled is True


# --------------------------------------------------------------------------
# read_registration: CS/EPS fallback
# --------------------------------------------------------------------------


async def test_cereg_home_is_registered():
    modem = _modem({"AT+CEREG?": _reg_response("AT+CEREG?", "+CEREG", 1)})
    assert await modem.read_registration() is True


async def test_cereg_roaming_is_registered():
    modem = _modem({"AT+CEREG?": _reg_response("AT+CEREG?", "+CEREG", 5)})
    assert await modem.read_registration() is True


async def test_falls_back_to_2g_when_eps_not_registered():
    # LTE says "not registered" but the SIM is up on 2G/CS — the whole point
    # of also checking +CREG.  This is the roaming-SIM case from the field.
    modem = _modem(
        {
            "AT+CEREG?": _reg_response("AT+CEREG?", "+CEREG", 0),
            "AT+CREG?": _reg_response("AT+CREG?", "+CREG", 1),
        }
    )
    assert await modem.read_registration() is True


async def test_neither_domain_registered_is_false():
    modem = _modem(
        {
            "AT+CEREG?": _reg_response("AT+CEREG?", "+CEREG", 0),
            "AT+CREG?": _reg_response("AT+CREG?", "+CREG", 0),
        }
    )
    assert await modem.read_registration() is False


async def test_read_registration_survives_a_failed_query():
    # A module that rejects AT+CEREG? must still be judged by AT+CREG?.
    modem = _modem(
        {
            "AT+CEREG?": ATError("not supported"),
            "AT+CREG?": _reg_response("AT+CREG?", "+CREG", 1),
        }
    )
    assert await modem.read_registration() is True


async def test_registration_domains_remain_visible_after_the_combined_read():
    modem = _modem(
        {
            "AT+CEREG?": _reg_response("AT+CEREG?", "+CEREG", 5),
            "AT+CREG?": _reg_response("AT+CREG?", "+CREG", 0),
        }
    )

    assert await modem.read_registration() is True
    assert modem.info.eps_registered is True
    assert modem.info.cs_registered is False


async def test_ims_registration_is_diagnostic_and_tri_state():
    registered = _modem(
        {"AT+CIREG?": _reg_response("AT+CIREG?", "+CIREG", 1)}
    )
    unavailable = _modem({"AT+CIREG?": ATError("not supported")})

    assert await registered.read_ims_registration() is True
    assert await unavailable.read_ims_registration() is None


async def test_operator_scan_and_selection_use_typed_commands():
    modem = _modem(
        {
            "AT+COPS=?": ATResponse(
                "AT+COPS=?",
                ['+COPS: (1,"CHINA MOBILE","CMCC","46000",7),(2,"Other","O","23420",0)'],
            ),
            'AT+COPS=1,2,"23420"': ATResponse(
                'AT+COPS=1,2,"23420"', []
            ),
            "AT+COPS?": ATResponse(
                "AT+COPS?", ['+COPS: 1,2,"23420",7']
            ),
        }
    )

    operators = await modem.scan_operators()
    assert [item["numeric"] for item in operators] == ["46000", "23420"]
    selected = await modem.select_operator("23420")
    assert selected["numeric"] == "23420"
    assert modem.operator_selection_mode == 1
    assert 'AT+COPS=1,2,"23420"' in modem.client.calls


async def test_select_operator_rejects_arbitrary_at_text():
    modem = _modem({})
    with pytest.raises(ValueError):
        await modem.select_operator("AT+CFUN=0")
    with pytest.raises(ValueError):
        await modem.select_operator("１２３４５")


async def test_select_operator_none_restores_automatic_selection():
    modem = _modem(
        {
            "AT+COPS=0": ATResponse("AT+COPS=0", []),
            "AT+COPS?": ATResponse("AT+COPS?", ["+COPS: 0"]),
        }
    )
    current = await modem.select_operator(None)
    assert current["mode"] == 0
    assert modem.operator_selection_mode == 0
    assert modem.client.calls[0] == "AT+COPS=0"


async def test_network_diagnostics_preserve_partial_firmware_support():
    modem = _modem(
        {
            "AT+CCED=0,1": ATResponse("AT+CCED=0,1", ["+CCED: raw"]),
            "AT+CCED=0,2": ATResponse("AT+CCED=0,2", ["+CCED: neighbour"]),
            "AT+EEMGINFO": ATError("not supported"),
            "AT*BANDIND?": ATResponse("AT*BANDIND?", ["*BANDIND: 0, 39, 7"]),
            "AT^SYSINFO": ATError("not supported"),
        }
    )
    assert await modem.read_network_diagnostics() == {
        "cced": {"lines": ["+CCED: raw"], "error": None},
        "cced_neighbors": {"lines": ["+CCED: neighbour"], "error": None},
        "eemginfo": {"lines": [], "error": "not supported"},
        "bandind": {"lines": ["*BANDIND: 0, 39, 7"], "error": None},
        "sysinfo": {"lines": [], "error": "not supported"},
    }


async def test_network_diagnostics_send_cced_with_parameters_only():
    """``AT+CCED`` has no bare execute form and mode 2 must never be sent.

    V1011 answers the parameterless spelling with ``+CME ERROR: 3``, which
    reads like a permission problem but only means the arguments are missing —
    the reason this read reported nothing for months.  Mode 2 stops periodic
    reporting rather than reading anything, so it stays off the wire.
    """
    modem = _modem(
        {
            "AT+CCED=0,1": ATResponse("AT+CCED=0,1", ["+CCED: serving"]),
            "AT+CCED=0,2": ATResponse("AT+CCED=0,2", ["+CCED: neighbour"]),
            "AT+EEMGINFO": ATError("not supported"),
            "AT*BANDIND?": ATResponse("AT*BANDIND?", []),
            "AT^SYSINFO": ATResponse("AT^SYSINFO", []),
        }
    )
    await modem.read_network_diagnostics()
    cced = [call for call in modem.client.calls if "CCED" in call]
    assert cced == ["AT+CCED=0,1", "AT+CCED=0,2"]


async def test_network_diagnostics_read_band_and_system_info_query_forms():
    """Only the query forms are sent — the set forms cannot lock a band.

    V1011 accepts ``*BANDIND=(0,1)`` and ``^SYSCONFIG`` mode ``(2)`` only, so
    neither expresses "lock to band N"; sending a set form here would change
    radio state for no gain.  Guard against a well-meant future edit.
    """
    modem = _modem(
        {
            "AT+CCED=0,1": ATResponse("AT+CCED=0,1", []),
            "AT+CCED=0,2": ATResponse("AT+CCED=0,2", []),
            "AT+EEMGINFO": ATResponse("AT+EEMGINFO", []),
            "AT*BANDIND?": ATResponse("AT*BANDIND?", ["*BANDIND: 0, 39, 7"]),
            "AT^SYSINFO": ATResponse("AT^SYSINFO", ["^SYSINFO: 2,2,1,17,1,7"]),
        }
    )
    await modem.read_network_diagnostics()
    assert "AT*BANDIND?" in modem.client.calls
    assert "AT^SYSINFO" in modem.client.calls
    assert not any(
        "BANDIND=" in call or "SYSCONFIG=" in call for call in modem.client.calls
    )


# --------------------------------------------------------------------------
# _on_registration: unsolicited reports are stat-first
# --------------------------------------------------------------------------


def test_urc_bare_stat_registers():
    modem = _modem({})
    modem._on_registration(ATUrc(name="+CREG", params="1"))
    assert modem.info.registered is True


def test_urc_mode2_with_location_registers():
    # "+CREG: 1,\"00C3\",\"1234ABCD\",7" — the location fields used to be read
    # as the stat and flip a registered module to unregistered.
    modem = _modem({})
    modem._on_registration(
        ATUrc(name="+CREG", params='1,"00C3","1234ABCD",7')
    )
    assert modem.info.registered is True


def test_urc_stat_zero_unregisters():
    modem = _modem({})
    modem.info.registered = True
    modem._on_registration(ATUrc(name="+CREG", params="0"))
    assert modem.info.registered is False


def test_urc_roaming_stat_registers():
    modem = _modem({})
    modem._on_registration(ATUrc(name="+CEREG", params='5,"1A2B","00C3F1D2",7'))
    assert modem.info.registered is True


def test_ims_urc_does_not_change_mobile_network_registration():
    modem = _modem({})
    modem.info.registered = True
    modem.info.eps_registered = True

    modem._on_registration(ATUrc(name="+CIREG", params="0"))

    assert modem.info.registered is True
    assert modem.info.ims_registered is False


def test_one_unregistered_network_urc_does_not_hide_the_other_domain():
    modem = _modem({})
    modem.info.eps_registered = True

    modem._on_registration(ATUrc(name="+CREG", params="0"))

    assert modem.info.registered is True
    assert modem.info.cs_registered is False


# --------------------------------------------------------------------------
# recover_registration
# --------------------------------------------------------------------------


async def test_recover_returns_early_when_cops_reattaches():
    # AT+COPS=0 alone brings it back — no radio cycle needed.
    modem = _modem(
        {
            "AT+COPS=0": ATResponse("AT+COPS=0", [], "OK"),
            "AT+CEREG?": _reg_response("AT+CEREG?", "+CEREG", 1),
        }
    )
    assert await modem.recover_registration() is True
    assert "AT+CFUN=0" not in modem.client.calls


async def test_recover_cycles_radio_when_cops_not_enough(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("air780e_agent.modem.asyncio.sleep", no_sleep)

    # First read (after COPS) still not registered; after the CFUN cycle it is.
    modem = _modem(
        {
            "AT+COPS=0": ATResponse("AT+COPS=0", [], "OK"),
            "AT+CFUN=0": ATResponse("AT+CFUN=0", [], "OK"),
            "AT+CFUN=1": ATResponse("AT+CFUN=1", [], "OK"),
            "AT+CEREG?": [
                _reg_response("AT+CEREG?", "+CEREG", 0),
                _reg_response("AT+CEREG?", "+CEREG", 1),
            ],
            "AT+CREG?": _reg_response("AT+CREG?", "+CREG", 0),
        }
    )
    assert await modem.recover_registration() is True
    assert "AT+CFUN=0" in modem.client.calls
    assert "AT+CFUN=1" in modem.client.calls
    assert modem.info.registered is True


async def test_recover_reports_failure_when_still_down(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("air780e_agent.modem.asyncio.sleep", no_sleep)

    modem = _modem(
        {
            "AT+COPS=0": ATResponse("AT+COPS=0", [], "OK"),
            "AT+CFUN=0": ATResponse("AT+CFUN=0", [], "OK"),
            "AT+CFUN=1": ATResponse("AT+CFUN=1", [], "OK"),
            "AT+CEREG?": _reg_response("AT+CEREG?", "+CEREG", 0),
            "AT+CREG?": _reg_response("AT+CREG?", "+CREG", 0),
        }
    )
    assert await modem.recover_registration() is False
    assert modem.info.registered is False


async def test_recovery_never_undoes_deliberate_flight_mode():
    modem = _modem({
        "AT+CFUN?": ATResponse("AT+CFUN?", ["+CFUN: 0"], "OK"),
    })

    assert await modem.recover_registration() is False
    assert "AT+COPS=0" not in modem.client.calls
    assert "AT+CFUN=1" not in modem.client.calls


async def test_recovery_keeps_flight_mode_when_radio_query_times_out():
    modem = _modem({"AT+CFUN?": ATError("query timed out")})
    modem.info.radio_enabled = False

    assert await modem.recover_registration() is False
    assert "AT+COPS=0" not in modem.client.calls
    assert "AT+CFUN=1" not in modem.client.calls


async def test_module_reset_uses_air780e_reset_command():
    modem = _modem({"AT+RESET": ATResponse("AT+RESET", [], "OK")})

    await modem.reset()

    assert modem.client.calls == ["AT+RESET"]


# --------------------------------------------------------------------------
# Firmware that answers AT+CEREG? under the +CGREG prefix (Air780E V1011)
# --------------------------------------------------------------------------


async def test_cereg_answered_as_cgreg_is_still_read():
    """V1011 answers AT+CEREG? with "+CGREG: 0,5" — measured on hardware.

    Matching only "+CEREG:" left eps_registered at None forever, so the device
    page could never show the LTE domain even while the module was attached.
    """
    modem = _modem({"AT+CEREG?": _reg_response("AT+CEREG?", "+CGREG", 5)})

    assert await modem.read_registration() is True
    assert modem.info.eps_registered is True


async def test_cgreg_alias_does_not_leak_into_the_cs_domain():
    """+CREG must not accept the +CGREG alias: they are different domains."""
    modem = _modem(
        {
            "AT+CEREG?": _reg_response("AT+CEREG?", "+CGREG", 0),
            "AT+CREG?": _reg_response("AT+CREG?", "+CGREG", 5),
        }
    )

    assert await modem.read_registration() is False
    assert modem.info.eps_registered is False
    assert modem.info.cs_registered is None


async def test_canonical_prefix_wins_when_both_appear():
    modem = _modem(
        {
            "AT+CEREG?": ATResponse(
                "AT+CEREG?", ["+CGREG: 0,0", "+CEREG: 0,5"], "OK"
            ),
        }
    )

    assert await modem.read_registration() is True
