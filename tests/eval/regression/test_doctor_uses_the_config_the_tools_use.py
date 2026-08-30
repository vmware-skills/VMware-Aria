"""The doctor must diagnose the config file the tools will actually load.

Real-hardware finding, 2026-08-30: with ``VMWARE_ARIA_CONFIG`` set,
``vmware-aria doctor`` reported every check PASS and exited 0, while the tools
failed — because the doctor was inspecting a different file.

``load_config`` resolves ``config_path or $VMWARE_ARIA_CONFIG or CONFIG_FILE``.
``run_doctor`` resolved ``config_path or CONFIG_FILE``, skipping the env var,
and then passed that path *explicitly* to ``load_config`` — which suppressed the
env var there too. So the doctor did not merely check a different file, it made
itself consistent with the wrong one, and produced a green report for a
configuration nothing else would ever read.

A diagnostic that green-lights a file the tools do not open is worse than no
diagnostic: it converts "my tools fail" into "my tools fail and the checker says
they should not", which is where the operator stops trusting the checker.

The precedence now lives in exactly one function, ``resolve_config_path``, that
both callers use — the two copies could not disagree slowly, which is how this
one drifted (CLAUDE.md 形态 #6).
"""

from __future__ import annotations

import pytest

from vmware_aria import config as cfg
from vmware_aria import doctor as doc

_MINIMAL = """
targets:
  lab:
    host: aria.example
    port: 443
    username: admin
"""


@pytest.mark.unit
def test_the_env_var_decides_which_file_is_resolved(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(_MINIMAL)
    monkeypatch.setenv("VMWARE_ARIA_CONFIG", str(elsewhere))

    assert cfg.resolve_config_path() == elsewhere


@pytest.mark.unit
def test_an_explicit_path_still_beats_the_env_var(tmp_path, monkeypatch):
    """The control on precedence: `--config` is the operator saying which file
    they mean, and it has to keep winning."""
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(_MINIMAL)
    monkeypatch.setenv("VMWARE_ARIA_CONFIG", str(tmp_path / "ignored.yaml"))

    assert cfg.resolve_config_path(explicit) == explicit


@pytest.mark.unit
def test_with_neither_it_is_the_default(monkeypatch):
    monkeypatch.delenv("VMWARE_ARIA_CONFIG", raising=False)

    assert cfg.resolve_config_path() == cfg.CONFIG_FILE


def _wide(monkeypatch) -> None:
    """Render the report wide enough that Rich does not elide the paths.

    At the default 80 columns Rich truncates a long detail with an ellipsis, so
    a tmp_path-length filename is genuinely absent from the output and the
    assertion below would be measuring the terminal, not the doctor. (Worth
    knowing on its own: in a narrow terminal this report really does hide the
    path it is talking about.)
    """
    monkeypatch.setenv("COLUMNS", "300")


def _flat(text: str) -> str:
    """The report with table drawing and line breaks removed."""
    return "".join(ch for ch in text if not ch.isspace() and ch not in "│┃")


@pytest.mark.unit
def test_doctor_fails_when_the_env_var_points_at_a_missing_file(
    tmp_path, monkeypatch, capsys
):
    """The reported failure. The default config may exist and be perfectly
    valid; it is not the file the tools will open."""
    missing = tmp_path / "not-there.yaml"
    monkeypatch.setenv("VMWARE_ARIA_CONFIG", str(missing))
    _wide(monkeypatch)

    ok = doc.run_doctor(skip_auth=True)
    out = capsys.readouterr().out

    assert ok is False, (
        "doctor passed against a config file that does not exist; every tool "
        "call will raise FileNotFoundError on this path"
    )
    assert str(missing) in _flat(out), (
        "the report must name the file it looked at — a green or red verdict "
        "about an unnamed file is what made this take a real estate to find"
    )


@pytest.mark.unit
def test_doctor_reads_the_env_vars_file_not_the_default(tmp_path, monkeypatch, capsys):
    """The positive half: pointed at a real file elsewhere, the doctor reports
    on that one."""
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(_MINIMAL)
    monkeypatch.setenv("VMWARE_ARIA_CONFIG", str(elsewhere))
    _wide(monkeypatch)

    doc.run_doctor(skip_auth=True)
    out = capsys.readouterr().out

    assert str(elsewhere) in _flat(out)
    assert "1target" in _flat(out), "it parsed the file it named"


@pytest.mark.unit
def test_load_config_and_the_doctor_cannot_disagree(tmp_path, monkeypatch):
    """The structural assertion, not a behavioural one: both go through the
    same resolver, so a future edit to one cannot silently desynchronise them.
    """
    import inspect

    for fn in (cfg.load_config, doc.run_doctor):
        source = inspect.getsource(fn)
        assert "resolve_config_path" in source, (
            f"{fn.__qualname__} resolves the config path by itself again; that "
            f"is the duplication this test exists to prevent"
        )
