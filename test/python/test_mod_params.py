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
