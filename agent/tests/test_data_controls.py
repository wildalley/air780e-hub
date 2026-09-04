"""Packet-data and roaming safety controls."""

from __future__ import annotations

from air780e_agent.at import ATError, ATResponse
from air780e_agent.modem import Air780E


class FakeClient:
    def __init__(self, responses: dict[str, ATResponse | ATError]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def register_urc(self, *_args, **_kwargs) -> None:
        pass

    async def execute(self, command: str, *, timeout=None, **_kwargs) -> ATResponse:
        self.calls.append(command)
        response = self.responses.get(command, ATResponse(command))
        if isinstance(response, ATError):
            raise response
        return response


def _modem(responses: dict[str, ATResponse | ATError]) -> tuple[Air780E, FakeClient]:
    client = FakeClient(responses)
    return Air780E(client), client


async def test_data_status_requires_both_attachment_and_pdp_to_be_off():
    modem, _ = _modem({
        "AT+CGATT?": ATResponse("AT+CGATT?", ["+CGATT: 1"]),
        "AT+CGACT?": ATResponse("AT+CGACT?", ["+CGACT: 1,1"]),
    })

    assert await modem.read_data_status() == (True, True)


async def test_disabling_data_deactivates_contexts_before_detaching():
    modem, client = _modem({
        "AT+CGACT=0": ATResponse("AT+CGACT=0"),
        "AT+CGATT=0": ATResponse("AT+CGATT=0"),
        "AT+CGATT?": ATResponse("AT+CGATT?", ["+CGATT: 0"]),
        "AT+CGACT?": ATResponse("AT+CGACT?", ["+CGACT: 1,0"]),
    })

    assert await modem.set_data_enabled(False) == (False, False)
    assert client.calls[:2] == ["AT+CGACT=0", "AT+CGATT=0"]


async def test_disabling_data_fails_if_the_modem_does_not_confirm_both_states_off():
    modem, _ = _modem({
        "AT+CGACT=0": ATResponse("AT+CGACT=0"),
        "AT+CGATT=0": ATResponse("AT+CGATT=0"),
        "AT+CGATT?": ATResponse("AT+CGATT?", ["+CGATT: 1"]),
        "AT+CGACT?": ATResponse("AT+CGACT?", ["+CGACT: 1,0"]),
    })

    try:
        await modem.set_data_enabled(False)
    except ATError as exc:
        assert "not confirmed" in str(exc)
    else:
        raise AssertionError("a positive data state must not be reported as off")


async def test_unknown_roaming_is_not_treated_as_safe_for_data():
    modem, _ = _modem({
        "AT+CEREG?": ATResponse("AT+CEREG?", []),
        "AT+CREG?": ATResponse("AT+CREG?", []),
    })

    assert await modem.read_registration_details() == (None, None, None)
