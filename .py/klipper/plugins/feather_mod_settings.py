## Lightweight helpers for the Feather mod-parameter editor.
##
## The helpers operate on objects already loaded by mod_params.py.  They do not
## parse the declaration again or retain a second copy of the parameter list.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import enum

from ui import NumericInputSpec


VISIBLE_ROWS = 5
MAX_VALUE_LENGTH = 64
RESTART_EFFECTS = frozenset(("klipper", "printer"))


def visible_parameters(manager):
    return [param for param in manager.params
            if (not getattr(param, "hidden", False)
                and parameter_is_visible(manager, param))]


def parameter_is_visible(manager, param):
    condition = getattr(param, "ui_visible_if", None)
    if not condition:
        return True
    if condition.get("operator") != "equals":
        return True
    parent_key = condition.get("parameter")
    parent = getattr(manager, "params_map", {}).get(parent_key)
    if parent is None:
        parent = next(
            (candidate for candidate in manager.params
             if candidate.key == parent_key), None)
    if parent is None:
        return True
    current = manager.variables.get(parent.key, parent.default)
    expected = condition.get("value")
    if parameter_kind(parent) == "enum":
        try:
            current = parent.type(current).name
        except (TypeError, ValueError):
            return False
    return current == expected


def parameter_category(manager, param):
    category_id = getattr(param, "ui_category", None)
    category = getattr(manager, "ui_categories_map", {}).get(category_id)
    return getattr(category, "label", None) or "OTHER"


def page_category_label(manager, parameters):
    labels = []
    for param in parameters:
        label = parameter_category(manager, param)
        if label not in labels:
            labels.append(label)
    return " > ".join(labels)


def parameter_kind(param):
    param_type = param.type
    if param_type is bool:
        return "bool"
    if isinstance(param_type, type) and issubclass(param_type, enum.Enum):
        return "enum"
    if param_type is int:
        return "int"
    if param_type is float:
        return "float"
    return "str"


def restart_effect(param):
    effect = getattr(param, "restart", None)
    return effect if effect in RESTART_EFFECTS else None


def numeric_input_spec(param):
    kind = parameter_kind(param)
    if kind not in ("int", "float"):
        raise TypeError("Parameter is not numeric")
    return NumericInputSpec(
        "integer" if kind == "int" else "decimal",
        minimum=getattr(param, "minimum", None),
        maximum=getattr(param, "maximum", None),
        max_length=MAX_VALUE_LENGTH,
        fraction_digits=(0 if kind == "int" else
                         getattr(param, "fraction_digits", None)))


def enum_names(param):
    return [member.name for member in param.type]


def current_edit_value(manager, param):
    value = manager.variables.get(param.key, param.default)
    if parameter_kind(param) == "enum":
        return param.type(value).name
    if parameter_kind(param) == "bool":
        return "1" if value else "0"
    return str(value)


def display_value(manager, param):
    kind = parameter_kind(param)
    value = manager.variables.get(param.key, param.default)
    if kind == "bool":
        return "ON" if value else "OFF"
    if kind == "enum":
        return param.type(value).name
    if kind == "float":
        return "%g" % value
    return str(value) if str(value) else "<EMPTY>"


def bool_display_active(param, raw_value):
    """Map a raw boolean to its visual toggle state without changing data."""
    if parameter_kind(param) != "bool":
        raise TypeError("Parameter is not boolean")
    return bool(raw_value) != bool(getattr(param, "ui_inverted", False))


def bool_labels(param):
    """Return the user-facing labels for false and true switch positions."""
    options = getattr(param, "options", None)
    if isinstance(options, (list, tuple)) and len(options) >= 2:
        return str(options[0]).upper(), str(options[1]).upper()
    return "OFF", "ON"


def description(param):
    value = getattr(param, "description", None)
    if value:
        return str(value)
    warning = getattr(param, "warning", None)
    if warning:
        return str(warning).splitlines()[0].lstrip("! ")
    return "Configure %s." % str(param.label).lower().rstrip(".")


def option_description(param, name):
    options = getattr(param, "options", None)
    if isinstance(options, dict):
        return str(options.get(name, ""))
    return ""


def validate_value(param, text):
    text = str(text)
    if len(text) > MAX_VALUE_LENGTH:
        raise ValueError("Value is too long")
    kind = parameter_kind(param)
    if kind in ("int", "float"):
        return numeric_input_spec(param).parse(text)
    if kind == "str":
        if any(ord(char) < 32 or ord(char) > 126 for char in text):
            raise ValueError("Only printable ASCII is supported")
        return text
    if kind == "bool":
        return bool(int(text))
    if text not in enum_names(param):
        raise ValueError("Unknown option")
    return text
