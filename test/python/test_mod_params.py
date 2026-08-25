"""Unit tests for the mod_params klippy plugin.

mod_params is the settings authority the rest of the mod reads through, so its
typing, persistence, and refusal behaviour are what matter. Each test here
targets a branch that can actually break.
"""

import pytest

import mod_params


def test_defaults_apply_without_touching_storage(stub_config, variables_path):
    """A fresh install resolves defaults in memory and writes nothing."""
    mgr = mod_params.ModParamManagement(stub_config)

    assert mgr.get_status(None)["variables"]["backlight"] == 50
    assert variables_path.read_text() == ""


def test_enum_default_resolves_to_its_int_value(stub_config):
    """DisplayEnum STOCK is declared as 0, and status exposes the int."""
    mgr = mod_params.ModParamManagement(stub_config)

    assert mgr.get_status(None)["variables"]["display"] == 0


def test_setting_a_value_persists_it(stub_config, variables_path, gcmd):
    mgr = mod_params.ModParamManagement(stub_config)

    mgr.cmd_SET_MOD_PARAM(gcmd(PARAM="backlight", VALUE="42"))

    assert mgr.get_status(None)["variables"]["backlight"] == 42
    assert "backlight = 42" in variables_path.read_text()


def test_enum_persists_as_its_name_not_its_int(stub_config, variables_path, gcmd):
    """Storage keeps the symbolic name, so a renumbered enum does not corrupt state."""
    mgr = mod_params.ModParamManagement(stub_config)

    mgr.cmd_SET_MOD_PARAM(gcmd(PARAM="display", VALUE="GUPPY"))

    assert "display = 'GUPPY'" in variables_path.read_text()


def test_setting_the_current_value_writes_nothing(stub_config, variables_path, gcmd):
    """Save-on-change only. Rewriting flash on every no-op set would be wasteful."""
    mgr = mod_params.ModParamManagement(stub_config)

    mgr.cmd_SET_MOD_PARAM(gcmd(PARAM="backlight", VALUE="50"))  # 50 is the default

    assert variables_path.read_text() == ""


@pytest.mark.parametrize("param,value", [("display", "NOPE"), ("backlight", "abc")])
def test_invalid_value_raises_and_does_not_persist(stub_config, variables_path,
                                                   gcmd, param, value):
    mgr = mod_params.ModParamManagement(stub_config)

    with pytest.raises(Exception) as excinfo:
        mgr.cmd_SET_MOD_PARAM(gcmd(PARAM=param, VALUE=value))

    # Pin the branch. A bare Exception would also be satisfied by an unrelated
    # TypeError from a broken stub, which would make this test meaningless.
    assert "Failed to update parameter" in str(excinfo.value)
    assert variables_path.read_text() == ""


def test_readonly_parameter_is_refused(readonly_config, gcmd):
    mgr = mod_params.ModParamManagement(readonly_config)

    with pytest.raises(Exception) as excinfo:
        mgr.cmd_SET_MOD_PARAM(gcmd(PARAM="ro", VALUE="9"))

    assert "readonly" in str(excinfo.value)


def test_near_miss_suggests_the_real_parameter(stub_config, gcmd):
    """A genuine typo gets a useful suggestion and does not raise."""
    mgr = mod_params.ModParamManagement(stub_config)
    cmd = gcmd(PARAM="backlite", VALUE="1")

    mgr.cmd_SET_MOD_PARAM(cmd)

    assert any("Unknown parameter" in m for m in cmd.raw)
    assert any("backlight" in m for m in cmd.info)


@pytest.mark.xfail(strict=True,
                   reason="_find_similar_param gates on `min_distance <= 10`, which "
                          "exceeds the length of most parameter names, so any short "
                          "typo (or plain garbage) always gets a suggestion and the "
                          "command returns success instead of raising")
def test_nonsense_input_is_not_given_a_suggestion(stub_config, gcmd):
    """`SET_MOD PARAM=zzzzzzzz` should say the parameter is unknown, not guess.

    Verified 2026-08-24: it answers "Did you mean display?", because the
    Levenshtein distance from 'zzzzzzzz' to 'display' is 8, which passes the
    <= 10 threshold.

    The no-match branch is not unreachable in general - against the real 41-key
    declaration, an input of 'z' * 20 does exceed the threshold and correctly
    raises. But any input in the size range of an actual parameter name always
    matches something, which is exactly the case a user hits.

    The consequence is worse than a bad hint: when a suggestion is found,
    cmd_SET_MOD_PARAM *returns* rather than raising, so a typo'd SET_MOD inside
    a macro silently succeeds and does nothing.

    strict=True deliberately: a non-strict xfail would XPASS silently forever
    once the threshold is fixed and nobody would remove the marker.
    """
    mgr = mod_params.ModParamManagement(stub_config)
    cmd = gcmd(PARAM="zzzzzzzz", VALUE="1")

    try:
        mgr.cmd_SET_MOD_PARAM(cmd)
    except Exception:
        return  # raising "Unknown parameter" is the correct behaviour

    assert not any("Did you mean" in m for m in cmd.info)
