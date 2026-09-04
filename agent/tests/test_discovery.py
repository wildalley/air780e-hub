"""Finding modules by identity rather than by which socket they are in.

The case that gives this module its reason to exist is
``test_swapping_the_usb_ports_does_not_swap_the_names``: with udev binding by
port path, moving two modules between sockets silently swaps which card the
agent thinks it is talking to.
"""

from __future__ import annotations

import pytest

from air780e_agent.config import DeviceConfig
from air780e_agent.discovery import NoSuchDevice, PortRegistry, ProbeResult


def fake_world(layout: dict[str, tuple[str, str]]):
    """Build a prober over ``{port: (imei, iccid)}``; unlisted ports stay mute."""
    seen: list[str] = []

    async def prober(port: str, *, timeout: float) -> ProbeResult | None:
        seen.append(port)
        if port not in layout:
            return None
        imei, iccid = layout[port]
        return ProbeResult(port=port, model="AirM2M_780E", imei=imei, iccid=iccid)

    prober.seen = seen  # type: ignore[attr-defined]
    return prober


@pytest.fixture
def patched_glob(monkeypatch):
    """Let a test declare which ports exist, without touching /dev."""
    ports: list[str] = []

    def set_ports(*names: str) -> None:
        ports[:] = list(names)

    monkeypatch.setattr(
        "air780e_agent.discovery.globmodule.glob", lambda pattern: list(ports)
    )
    return set_ports


async def test_claims_the_port_whose_imei_matches(patched_glob):
    patched_glob("/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM2": ("863304089655700", "8964052040035110800F"),
    }))

    port = await registry.acquire(DeviceConfig(name="a", imei="863304089655700"))
    assert port == "/dev/ttyACM2"
    assert registry.claimed_by("/dev/ttyACM2") == "a"


async def test_claims_the_port_whose_iccid_matches(patched_glob):
    patched_glob("/dev/ttyACM0", "/dev/ttyACM1")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("863304089655700", "8964052040035110800F"),
        "/dev/ttyACM1": ("863304089655701", "8964052040035110801F"),
    }))

    port = await registry.acquire(
        DeviceConfig(name="b", iccid="8964052040035110801F")
    )
    assert port == "/dev/ttyACM1"


async def test_swapping_the_usb_ports_does_not_swap_the_names(patched_glob):
    """The whole point: identity travels with the module, not the socket."""
    a = DeviceConfig(name="a", imei="863304089655700")
    b = DeviceConfig(name="b", imei="863304089655701")

    patched_glob("/dev/ttyACM0", "/dev/ttyACM1")
    before = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("863304089655700", ""),
        "/dev/ttyACM1": ("863304089655701", ""),
    }))
    assert await before.acquire(a) == "/dev/ttyACM0"
    assert await before.acquire(b) == "/dev/ttyACM1"

    # Same two modules, sockets exchanged.
    after = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("863304089655701", ""),
        "/dev/ttyACM1": ("863304089655700", ""),
    }))
    assert await after.acquire(a) == "/dev/ttyACM1"
    assert await after.acquire(b) == "/dev/ttyACM0"


async def test_a_claimed_port_is_not_offered_twice(patched_glob):
    patched_glob("/dev/ttyACM0", "/dev/ttyACM1")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("863304089655700", ""),
        "/dev/ttyACM1": ("863304089655701", ""),
    }))

    await registry.acquire(DeviceConfig(name="a", imei="863304089655700"))
    # Asking for the same module again must not hand out its port a second
    # time — two clients on one AT port interleave and corrupt each other.
    with pytest.raises(NoSuchDevice):
        await registry.acquire(DeviceConfig(name="clone", imei="863304089655700"))


async def test_a_released_port_can_be_claimed_again(patched_glob):
    patched_glob("/dev/ttyACM0")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("863304089655700", ""),
    }))
    config = DeviceConfig(name="a", imei="863304089655700")

    port = await registry.acquire(config)
    registry.release(port)
    assert registry.claimed_by(port) is None
    assert await registry.acquire(config) == "/dev/ttyACM0"


async def test_ports_that_do_not_answer_are_skipped(patched_glob):
    """Two of the three ACM interfaces are not AT ports; a module on LuatOS
    firmware answers on none of them."""
    patched_glob("/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2")
    prober = fake_world({"/dev/ttyACM2": ("863304089655700", "")})
    registry = PortRegistry(prober=prober)

    assert await registry.acquire(
        DeviceConfig(name="a", imei="863304089655700")
    ) == "/dev/ttyACM2"
    assert prober.seen == ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2"]


async def test_a_missing_module_is_an_error_not_a_wrong_guess(patched_glob):
    """Adopting whatever is present would send keep-alive SMS from the wrong
    card — a plain failure is the safer outcome."""
    patched_glob("/dev/ttyACM0")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("863304089655701", ""),
    }))

    with pytest.raises(NoSuchDevice, match="not found"):
        await registry.acquire(DeviceConfig(name="a", imei="863304089655700"))
    assert registry.claimed_by("/dev/ttyACM0") is None


async def test_no_ports_at_all_says_so(patched_glob):
    patched_glob()
    registry = PortRegistry(prober=fake_world({}))
    with pytest.raises(NoSuchDevice, match="no unclaimed port"):
        await registry.acquire(DeviceConfig(name="a", imei="8633"))


async def test_the_only_module_needs_no_identity(patched_glob):
    patched_glob("/dev/ttyACM0", "/dev/ttyACM1")
    registry = PortRegistry(
        prober=fake_world({"/dev/ttyACM1": ("863304089655700", "")}),
        sole_device=True,
    )
    assert await registry.acquire(DeviceConfig(name="a")) == "/dev/ttyACM1"


async def test_both_identifiers_must_agree(patched_glob):
    """Giving both means 'this card, in this module' — a half match is a
    module whose SIM was swapped, which is exactly what to catch."""
    patched_glob("/dev/ttyACM0")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("863304089655700", "8964052040035110899F"),
    }))

    with pytest.raises(NoSuchDevice):
        await registry.acquire(DeviceConfig(
            name="a",
            imei="863304089655700",
            iccid="8964052040035110800F",  # a different card
        ))


async def test_identity_comparison_ignores_case_and_padding(patched_glob):
    patched_glob("/dev/ttyACM0")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("863304089655700", "8964052040035110800f"),
    }))
    port = await registry.acquire(
        DeviceConfig(name="a", iccid="  8964052040035110800F  ")
    )
    assert port == "/dev/ttyACM0"


# --------------------------------------------------------------------------
# survey: the autodetect half, which starts from the module and asks whether
# anybody has a claim on it
# --------------------------------------------------------------------------


async def test_survey_reports_a_module_nobody_configured(patched_glob):
    patched_glob("/dev/ttyACM0")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("862323088372050", "8944110068802881339F"),
    }))

    found = await registry.survey([])
    assert [r.imei for r in found] == ["862323088372050"]


async def test_survey_deduplicates_multiple_at_ports_of_one_module(patched_glob):
    """An Air780E may answer on more than one ACM interface."""
    patched_glob("/dev/ttyACM3", "/dev/ttyACM5", "/dev/ttyACM7")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM3": ("862323088372043", "8964012207010569008F"),
        "/dev/ttyACM5": ("862323088372043", "8964012207010569008F"),
        "/dev/ttyACM7": ("862323088372050", "8944110068802881339F"),
    }))

    found = await registry.survey([])

    assert [result.imei for result in found] == [
        "862323088372043",
        "862323088372050",
    ]


async def test_survey_leaves_a_configured_module_alone(patched_glob):
    """The module is present and its device block is waiting for it: adopting
    it would take the name that device is entitled to."""
    patched_glob("/dev/ttyACM0")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("862323088372050", ""),
    }))

    assert await registry.survey([
        DeviceConfig(name="b", imei="862323088372050")
    ]) == []


async def test_survey_reports_only_the_unconfigured_module(patched_glob):
    patched_glob("/dev/ttyACM0", "/dev/ttyACM1")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("863304089655700", ""),
        "/dev/ttyACM1": ("862323088372050", ""),
    }))

    found = await registry.survey([DeviceConfig(name="a", imei="863304089655700")])
    assert [r.port for r in found] == ["/dev/ttyACM1"]


async def test_survey_skips_ports_already_claimed(patched_glob):
    patched_glob("/dev/ttyACM0", "/dev/ttyACM1")
    prober = fake_world({
        "/dev/ttyACM0": ("863304089655700", ""),
        "/dev/ttyACM1": ("862323088372050", ""),
    })
    registry = PortRegistry(prober=prober)
    await registry.acquire(DeviceConfig(name="a", imei="863304089655700"))

    prober.seen.clear()
    found = await registry.survey([DeviceConfig(name="a", imei="863304089655700")])
    assert [r.port for r in found] == ["/dev/ttyACM1"]
    # A port another worker owns must not even be opened: a second AT client on
    # it would interleave with the first.
    assert prober.seen == ["/dev/ttyACM1"]


async def test_survey_does_not_open_a_pinned_port(patched_glob, monkeypatch):
    """A pinned worker bypasses the registry, so its port is never in
    ``_claimed`` — but it is very much in use."""
    monkeypatch.setattr("air780e_agent.discovery.os.path.realpath", lambda p: p)
    patched_glob("/dev/ttyACM0", "/dev/ttyACM1")
    prober = fake_world({
        "/dev/ttyACM0": ("863304089655700", ""),
        "/dev/ttyACM1": ("862323088372050", ""),
    })
    registry = PortRegistry(prober=prober)

    found = await registry.survey([DeviceConfig(name="a", port="/dev/ttyACM0")])
    assert [r.port for r in found] == ["/dev/ttyACM1"]
    assert prober.seen == ["/dev/ttyACM1"]


async def test_survey_resolves_a_pinned_symlink(patched_glob, monkeypatch):
    """Configs pin udev symlinks; the glob yields the tty they point at."""
    monkeypatch.setattr(
        "air780e_agent.discovery.os.path.realpath",
        lambda p: "/dev/ttyACM0" if p == "/dev/air780e-a" else p,
    )
    patched_glob("/dev/ttyACM0")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("863304089655700", ""),
    }))

    assert await registry.survey([
        DeviceConfig(name="a", port="/dev/air780e-a")
    ]) == []


async def test_survey_does_not_claim_what_it_finds(patched_glob):
    """Surveying is a question, not a decision — the port must stay available
    to whichever worker ends up wanting it."""
    patched_glob("/dev/ttyACM0")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM0": ("862323088372050", ""),
    }))

    await registry.survey([])
    assert registry.claimed_by("/dev/ttyACM0") is None
    assert await registry.acquire(
        DeviceConfig(name="adopted", imei="862323088372050")
    ) == "/dev/ttyACM0"


async def test_survey_ignores_mute_ports(patched_glob):
    patched_glob("/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2")
    registry = PortRegistry(prober=fake_world({
        "/dev/ttyACM2": ("862323088372050", ""),
    }))

    found = await registry.survey([])
    assert [r.port for r in found] == ["/dev/ttyACM2"]
