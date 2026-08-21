#!/usr/bin/env python3
## Small zero-dependency local web editor for Feather UI themes.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

"""Small zero-dependency local web editor for Feather UI themes.

Typical zero-config use when this file itself lives in the themes directory:

    cd themes
    python3 feather_theme_editor_20260817_1532.py

The editor:
- discovers theme JSON files in the current directory;
- finds theme.schema.json next to them;
- searches upward for theme.py / framework/ui/theme.py;
- serves on 127.0.0.1 by default, with configurable bind and URL hosts;
- opens the browser unless --no-open is supplied;
- generates light or dark palettes from common color harmonies;
- keeps generated themes in local browser storage;
- edits ThemeColor and ThemeRole values live;
- validates and downloads a custom theme JSON.

No mandatory third-party dependencies are required. If jsonschema is installed,
theme.schema.json is additionally used for validation.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")

FALLBACK_COLORS = (
    "background", "panel", "primary", "primary_dark", "secondary",
    "secondary_dark", "warning", "danger", "danger_background", "text",
    "bright", "dim", "border", "muted", "success", "pressed_background",
    "overlay",
)

FALLBACK_ROLES = (
    "button_background", "button_border", "button_text",
    "button_selected_background", "button_selected_border",
    "button_selected_text", "accent_background", "accent_border",
    "accent_text", "header_background", "header_text",
    "header_border", "temperature_nozzle", "temperature_bed",
    "temperature_fan",
)

FALLBACK_DEFAULTS = {
    "button_background": "panel",
    "button_border": "primary",
    "button_text": "primary",
    "button_selected_background": "panel",
    "button_selected_border": "secondary",
    "button_selected_text": "secondary",
    "accent_background": "primary_dark",
    "accent_border": "primary",
    "accent_text": "bright",
    "header_background": "panel",
    "header_text": "primary",
    "header_border": "border",
    "temperature_nozzle": "primary",
    "temperature_bed": "primary",
    "temperature_fan": "primary",
}


def normalize_hex(value):
    value = str(value).strip().lower().lstrip("#")
    if HEX_RE.fullmatch(value) is None:
        raise ValueError("expected six-digit HEX")
    return value


def parse_contract_source(path):
    source = Path(path).read_text(encoding="utf-8")

    color_match = re.search(
        r"class\s+ThemeColor\s*\([^)]*\)\s*:(.*?)(?=\nclass\s|\Z)",
        source, re.S)
    colors = tuple(re.findall(
        r'^\s+[A-Z][A-Z0-9_]*\s*=\s*"([^"]+)"',
        color_match.group(1), re.M)) if color_match else ()

    role_match = re.search(
        r"class\s+ThemeRole\s*\([^)]*\)\s*:(.*?)(?=\n(?:class|DEFAULT_THEME_ROLES)\b|\Z)",
        source, re.S)
    if role_match:
        role_body = role_match.group(1)
    else:
        # Also tolerate an in-progress source where the class name was lost but
        # the enum members and DEFAULT_THEME_ROLES are still present.
        anchor = source.find('BUTTON_BACKGROUND = "button_background"')
        if anchor >= 0:
            start = source.rfind("class ", 0, anchor)
            end = source.find("DEFAULT_THEME_ROLES", anchor)
            role_body = source[start:end if end >= 0 else None]
        else:
            role_body = ""

    roles = tuple(re.findall(
        r'^\s+[A-Z][A-Z0-9_]*\s*=\s*"([^"]+)"',
        role_body, re.M))

    defaults = {}
    if color_match and role_body:
        for role_symbol, color_symbol in re.findall(
                r"ThemeRole\.([A-Z][A-Z0-9_]*)\s*:\s*ThemeColor\.([A-Z][A-Z0-9_]*)",
                source):
            role_name = re.search(
                r'^\s*%s\s*=\s*"([^"]+)"' % re.escape(role_symbol),
                role_body, re.M)
            color_name = re.search(
                r'^\s*%s\s*=\s*"([^"]+)"' % re.escape(color_symbol),
                color_match.group(1), re.M)
            if role_name and color_name:
                defaults[role_name.group(1)] = color_name.group(1)

    if not colors or not roles or any(role not in defaults for role in roles):
        raise RuntimeError("could not recover complete theme contract")

    return {
        "colors": colors,
        "roles": roles,
        "defaults": defaults,
        "mode": "parsed-source",
    }


def load_contract(theme_py):
    if theme_py is None or not Path(theme_py).is_file():
        return {
            "colors": FALLBACK_COLORS,
            "roles": FALLBACK_ROLES,
            "defaults": dict(FALLBACK_DEFAULTS),
            "mode": "embedded",
        }

    path = Path(theme_py)
    try:
        spec = importlib.util.spec_from_file_location(
            "feather_theme_editor_contract", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {
            "colors": tuple(item.value for item in module.ThemeColor),
            "roles": tuple(item.value for item in module.ThemeRole),
            "defaults": {
                role.value: module.DEFAULT_THEME_ROLES[role].value
                for role in module.ThemeRole
            },
            "mode": "imported",
        }
    except Exception:
        try:
            return parse_contract_source(path)
        except Exception:
            return {
                "colors": FALLBACK_COLORS,
                "roles": FALLBACK_ROLES,
                "defaults": dict(FALLBACK_DEFAULTS),
                "mode": "embedded-fallback",
            }


def directory_has_themes(path):
    if not path.is_dir():
        return False

    for candidate in path.glob("*.json"):
        if candidate.name == "theme.schema.json":
            continue
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(document, dict) and isinstance(document.get("colors"), dict):
            return True

    return False


def discover_themes_dir(base, explicit):
    if explicit is not None:
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir():
            raise SystemExit("--themes-dir is not a directory: %s" % path)
        return path

    if directory_has_themes(base):
        return base

    child = base / "themes"
    if directory_has_themes(child):
        return child

    return base


def search_roots(base, max_parents=6):
    roots = [base]
    current = base
    for _index in range(max_parents):
        parent = current.parent
        if parent == current:
            break
        roots.append(parent)
        current = parent
    return roots


def discover_theme_py(base):
    for root in search_roots(base):
        for candidate in (
                root / "theme.py",
                root / "framework" / "ui" / "theme.py",
                root / "src" / "theme.py"):
            if candidate.is_file():
                return candidate
    return None


def discover_schema(base, themes_dir):
    direct = themes_dir / "theme.schema.json"
    if direct.is_file():
        return direct

    for root in search_roots(base):
        for candidate in (
                root / "theme.schema.json",
                root / "themes" / "theme.schema.json"):
            if candidate.is_file():
                return candidate
    return None


def load_theme_files(themes_dir):
    result = []
    for path in sorted(themes_dir.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(document, dict) and isinstance(document.get("colors"), dict):
            result.append({
                "file": path.name,
                "name": str(document.get("name") or path.stem),
                "description": str(document.get("description") or ""),
            })
    return result


def validate_basic(document, contract):
    errors = []
    if not isinstance(document, dict):
        return ["Theme must be a JSON object."]

    if not str(document.get("name") or "").strip():
        errors.append("name is required")

    colors = document.get("colors")
    if not isinstance(colors, dict):
        errors.append("colors must be an object")
        colors = {}

    expected_colors = set(contract["colors"])
    actual_colors = set(colors)
    for item in sorted(expected_colors - actual_colors):
        errors.append("missing color: %s" % item)
    for item in sorted(actual_colors - expected_colors):
        errors.append("unknown color: %s" % item)

    for key, value in colors.items():
        try:
            normalize_hex(value)
        except Exception:
            errors.append("%s must be six-digit HEX" % key)

    roles = document.get("roles") or {}
    if not isinstance(roles, dict):
        errors.append("roles must be an object")
        roles = {}

    expected_roles = set(contract["roles"])
    color_names = set(contract["colors"])
    for item in sorted(set(roles) - expected_roles):
        errors.append("unknown role: %s" % item)

    for key, value in roles.items():
        raw = str(value).strip().lower().lstrip("#")
        if raw not in color_names and HEX_RE.fullmatch(raw) is None:
            errors.append(
                "%s must reference ThemeColor or contain six-digit HEX" % key)

    return errors


def schema_validate(document, schema_path):
    if schema_path is None:
        return [], False

    try:
        import jsonschema
    except Exception:
        return [], False

    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        jsonschema.validate(document, schema)
        return [], True
    except Exception as exc:
        return ["schema: %s" % exc], True


def safe_filename(name):
    value = str(name).strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip("-._")
    return value or "custom-theme"


HTML_PAGE = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Feather Theme Editor</title>
<style>
:root {
  color-scheme: dark;
  --app-bg:#121417;
  --app-panel:#1b1e22;
  --app-panel2:#22262b;
  --app-border:#363c44;
  --app-text:#e8edf2;
  --app-dim:#9aa4ae;
  --app-accent:#69c9ff;
  --ok:#68d391;
  --bad:#ff6b6b;
}
* { box-sizing:border-box; }
html,body {
  margin:0; width:100%; height:100%; overflow:hidden;
  background:var(--app-bg); color:var(--app-text);
}
body { font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
button,input,select,textarea { font:inherit; }
button { cursor:pointer; }
.app {
  max-width:1760px; height:100vh; margin:0 auto; padding:16px 22px 22px;
  display:grid; grid-template-rows:auto auto minmax(0,1fr); gap:12px;
}
.top {
  display:flex; gap:16px; align-items:end; justify-content:space-between;
  flex-wrap:wrap;
}
h1 { margin:0; font-size:24px; }
.sub { color:var(--app-dim); }
.toolbar { display:flex; gap:10px; align-items:end; flex-wrap:wrap; }
.field { display:grid; gap:5px; }
.field label { color:var(--app-dim); font-size:12px; }
.field input[type=text], .field select {
  color:var(--app-text); background:var(--app-panel);
  border:1px solid var(--app-border); padding:8px 10px; min-height:38px;
}
.name-field input { min-width:230px; }
.desc-field input { min-width:320px; }
.primary-action {
  min-height:38px; padding:8px 14px; color:#071018;
  background:var(--app-accent); border:0; font-weight:700;
}
.primary-action:disabled { opacity:.35; cursor:not-allowed; }
.generate-action {
  min-height:38px; padding:8px 15px; color:var(--app-accent);
  background:var(--app-panel2); border:1px solid var(--app-accent); font-weight:700;
}
.theme-select-wrap { display:flex; gap:6px; }
.theme-select-wrap select { min-width:180px; }
.delete-theme {
  width:38px; min-height:38px; padding:0; color:var(--bad);
  background:var(--app-panel2); border:1px solid #6b3535; font-size:20px;
}
.delete-theme[hidden] { display:none; }
.status {
  border:1px solid var(--app-border); background:var(--app-panel);
  padding:9px 12px; color:var(--app-dim); min-height:38px;
}
.status.ok { color:var(--ok); border-color:#355c45; }
.status.bad { color:var(--bad); border-color:#6b3535; }
.layout {
  min-height:0; overflow:hidden;
  display:grid; grid-template-columns:minmax(410px,.9fr) minmax(640px,1.6fr);
  gap:18px; align-items:stretch;
}
.editor-stack,.preview-wrap {
  min-height:0; overflow-y:auto; overscroll-behavior:contain;
  scrollbar-gutter:stable; padding-right:6px;
  display:grid; gap:18px; align-content:start;
}
.panel { background:var(--app-panel); border:1px solid var(--app-border); }
.panel h2 {
  margin:0; padding:13px 15px;
  border-bottom:1px solid var(--app-border); font-size:15px;
}
.panel-body { padding:14px; }
.color-grid { display:grid; gap:7px; }
.color-row {
  display:grid; grid-template-columns:175px 48px 1fr;
  gap:8px; align-items:center;
}
.color-row code,.role-row code { color:var(--app-dim); }
.color-row input[type=color],.role-color {
  width:48px; height:34px; padding:2px;
  background:transparent; border:1px solid var(--app-border);
}
.hex-input {
  width:100%; min-width:0; color:var(--app-text); background:var(--app-panel2);
  border:1px solid var(--app-border); padding:7px 8px;
}
.role-grid { display:grid; gap:8px; }
.role-row {
  display:grid; grid-template-columns:220px 1fr 112px;
  gap:8px; align-items:center;
}
.role-row select,.role-row input[type=text] {
  color:var(--app-text); background:var(--app-panel2);
  border:1px solid var(--app-border); padding:7px 8px; min-width:0;
}
.role-color { display:none; width:112px; }
.role-row.custom .role-color { display:block; }
.note { color:var(--app-dim); font-size:12px; margin-top:8px; }
.preview {
  background:var(--c-background); color:var(--c-text);
  border:1px solid var(--c-border); padding:18px;
}
.preview-title { color:var(--diag-background); font-size:12px; margin-bottom:10px; }
.preview-header {
  background:var(--r-header-background); color:var(--r-header-text);
  border:2px solid var(--r-header-border); padding:16px;
  text-align:center; font-size:20px; margin-bottom:16px;
}
.preview-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.preview-card {
  background:var(--c-panel); border:1px solid var(--c-border); padding:12px;
  min-height:120px;
}
.preview-card h3 { margin:0 0 10px; color:var(--diag-panel); font-size:12px; }
.demo-button {
  width:100%; min-height:58px; background:var(--r-button-background);
  color:var(--r-button-text); border:2px solid var(--r-button-border);
}
.demo-button.selected {
  background:var(--r-button-selected-background);
  color:var(--r-button-selected-text);
  border-color:var(--r-button-selected-border);
}
.temps { display:grid; gap:8px; }
.temp-nozzle { color:var(--r-temperature-nozzle); }
.temp-bed { color:var(--r-temperature-bed); }
.temp-fan { color:var(--r-temperature-fan); }
.statuses { display:flex; gap:8px; flex-wrap:wrap; }
.chip { background:var(--c-panel); border:1px solid currentColor; padding:7px 9px; }
.warning { color:var(--c-warning); }
.danger { color:var(--c-danger); }
.success { color:var(--c-success); }
.tokens { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
.token {
  min-height:58px; border:1px solid var(--c-border); padding:8px;
  display:grid; align-content:space-between;
}
.token span { font-size:11px; }
.t-text { color:var(--c-text); }
.t-bright { color:var(--c-bright); }
.t-primary { color:var(--c-primary); }
.t-secondary { color:var(--c-secondary); }
.t-dim { color:var(--c-dim); }
.t-muted { color:var(--c-muted); }
.runtime-block { margin-top:14px; padding-top:14px; border-top:1px solid var(--c-border); }
.runtime-block > h3 { margin:0 0 10px; color:var(--diag-background); font-size:12px; }
.runtime-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
.runtime-item { background:var(--c-panel); border:1px solid var(--c-border); padding:9px; }
.runtime-head {
  display:flex; gap:8px; align-items:center; justify-content:space-between;
  color:var(--diag-panel); font-size:10px; margin-bottom:7px;
}
.runtime-ratio { border:1px solid currentColor; padding:2px 5px; white-space:nowrap; }
.runtime-sample {
  min-height:48px; display:flex; align-items:center; justify-content:center;
  border:2px solid transparent; padding:8px; font-weight:700; text-align:center;
}
.runtime-detail { color:var(--diag-panel); opacity:.75; font-size:9px; margin-top:6px; }
.palette-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
.swatch {
  min-height:82px; padding:8px; border:1px solid var(--app-border);
  display:grid; align-content:space-between;
}
.swatch b,.swatch span { font-size:11px; }
.swatch .meta { font-size:9px; opacity:.8; }
.physical-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
.physical-item {
  min-height:86px; background:var(--app-panel2); border:1px solid var(--app-border);
  padding:8px; display:grid; grid-template-columns:70px 1fr; gap:9px;
}
.physical-color {
  min-height:68px; border:1px solid var(--app-border); display:flex;
  align-items:end; justify-content:center; padding:5px; font-size:9px;
}
.physical-meta { font-size:9px; color:var(--app-dim); overflow-wrap:anywhere; }
.physical-meta b { color:var(--app-text); font-size:10px; }
.role-combinations { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
.role-combination {
  border:1px solid var(--app-border); background:var(--app-panel2);
  padding:10px; min-width:0;
}
.role-combination-head {
  display:flex; align-items:baseline; justify-content:space-between;
  gap:8px; margin-bottom:8px;
}
.role-combination-head h3 { margin:0; color:var(--app-text); font-size:11px; }
.role-combination-head span { color:var(--app-dim); font-size:9px; }
.role-combination-sample {
  min-height:54px; display:flex; align-items:center; justify-content:center;
  padding:7px; margin-bottom:9px;
}
.role-mini-button { width:100%; min-height:42px; font-weight:700; }
.role-mini-header {
  width:100%; min-height:42px; display:flex; align-items:center;
  justify-content:center; font-weight:700;
}
.role-mini-temperatures {
  width:100%; min-height:42px; display:grid; grid-template-columns:repeat(3,1fr);
  align-items:center; gap:6px; padding:0 10px; font-weight:700;
  background:var(--c-panel); border:1px solid var(--c-border); text-align:center;
}
.role-combination-colors {
  display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:6px;
}
.role-color-item {
  min-width:0; padding:7px; border:1px solid var(--app-border);
  background:var(--app-panel);
}
.role-color-head,.role-color-value {
  display:flex; align-items:center; justify-content:space-between; gap:5px;
}
.role-color-head { margin-bottom:6px; }
.role-color-head b { color:var(--app-text); font-size:9px; }
.role-source-badge {
  padding:1px 4px; border:1px solid var(--app-border); color:var(--app-dim);
  font-size:8px; line-height:1.35; white-space:nowrap;
}
.role-source-badge.override { color:var(--app-accent); border-color:var(--app-accent); }
.role-color-value { justify-content:flex-start; color:var(--app-text); font-size:9px; }
.role-color-value code { overflow-wrap:anywhere; }
.role-chip-color {
  width:22px; height:22px; padding:0; border:1px solid var(--app-border);
  border-radius:2px; flex:0 0 auto; cursor:pointer; background:transparent;
}
.role-chip-color::-webkit-color-swatch-wrapper { padding:1px; }
.role-chip-color::-webkit-color-swatch { border:0; }
.role-chip-color::-moz-color-swatch { border:0; }
.role-color-value:hover .role-chip-color { outline:2px solid var(--app-accent); outline-offset:1px; }
.role-color-meta {
  display:grid; gap:2px; margin-top:5px; color:var(--app-dim);
  font-size:8px; overflow-wrap:anywhere;
}
.json-box {
  width:100%; min-height:250px; resize:vertical; color:var(--app-text);
  background:#0c0f12; border:1px solid var(--app-border); padding:12px;
  white-space:pre; overflow:auto;
}
.small-actions { display:flex; gap:8px; margin-top:10px; }
.secondary-action {
  padding:7px 10px; color:var(--app-text); background:var(--app-panel2);
  border:1px solid var(--app-border);
}
.generator-dialog {
  width:min(680px,calc(100vw - 32px)); max-height:calc(100vh - 32px);
  padding:0; color:var(--app-text); background:var(--app-panel);
  border:1px solid var(--app-border); box-shadow:0 24px 80px #000b;
}
.generator-dialog::backdrop { background:#05080bcc; }
.generator-head {
  display:flex; justify-content:space-between; align-items:start; gap:16px;
  padding:18px 20px; border-bottom:1px solid var(--app-border);
}
.generator-head h2 { margin:0 0 4px; font-size:19px; }
.generator-close {
  width:34px; height:34px; color:var(--app-dim); background:transparent;
  border:1px solid var(--app-border); font-size:20px; line-height:1;
}
.generator-body { display:grid; gap:16px; padding:20px; overflow:auto; }
.generator-row { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
.generator-color { display:grid; grid-template-columns:48px 1fr; gap:8px; }
.generator-color input[type=color] {
  width:48px; height:38px; padding:2px; background:transparent;
  border:1px solid var(--app-border);
}
.generator-mode { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.generator-mode label {
  display:flex; align-items:center; justify-content:center; gap:8px;
  min-height:38px; padding:8px; border:1px solid var(--app-border);
  background:var(--app-panel2); cursor:pointer;
}
.generator-mode label:has(input:checked) { border-color:var(--app-accent); color:var(--app-accent); }
.generator-palette { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
.generator-swatch {
  min-height:76px; padding:8px; border:1px solid var(--app-border);
  display:grid; align-content:space-between; font-size:11px;
  position:relative; cursor:pointer;
}
.generator-swatch:hover,.generator-swatch:focus-within {
  outline:2px solid var(--app-accent); outline-offset:1px;
}
.generator-swatch input[type=color] {
  position:absolute; inset:0; width:100%; height:100%; opacity:0; cursor:pointer;
}
.generator-swatch .edit-hint { font-size:9px; opacity:.75; }
.generator-footer {
  display:flex; justify-content:flex-end; gap:9px; padding:15px 20px;
  border-top:1px solid var(--app-border);
}
@media (max-width:1100px) {
  html,body { height:auto; overflow:auto; }
  .app { height:auto; min-height:100vh; display:block; }
  .top,.status { margin-bottom:12px; }
  .layout { grid-template-columns:1fr; overflow:visible; }
  .editor-stack,.preview-wrap { overflow:visible; min-height:auto; padding-right:0; }
  .preview-wrap { margin-top:18px; }
}
@media (max-width:720px) {
  .app { padding:12px; }
  .generator-row { grid-template-columns:1fr; }
  .color-row { grid-template-columns:1fr 48px 120px; }
  .role-row { grid-template-columns:1fr; }
  .preview-grid,.runtime-grid,.role-combinations,.physical-grid { grid-template-columns:1fr; }
  .palette-grid { grid-template-columns:repeat(2,1fr); }
}
</style>
</head>
<body>
<div class="app">
  <div class="top">
    <h1>Feather Theme Editor</h1>
    <div class="toolbar">
      <button id="generateButton" class="generate-action">Generate</button>
      <div class="field">
        <label>Theme / starting point</label>
        <div class="theme-select-wrap">
          <select id="themeSelect"></select>
          <button id="deleteTheme" class="delete-theme" title="Delete saved theme"
            aria-label="Delete saved theme" hidden>×</button>
        </div>
      </div>
      <div class="field name-field">
        <label>Custom theme name</label>
        <input id="themeName" type="text" placeholder="MY THEME">
      </div>
      <div class="field desc-field">
        <label>Description</label>
        <input id="themeDescription" type="text" placeholder="Custom palette">
      </div>
      <button id="downloadButton" class="primary-action">Download JSON</button>
    </div>
  </div>

  <div id="validationStatus" class="status">Waiting for theme…</div>

  <div class="layout">
    <div class="editor-stack">
      <section class="panel">
        <h2>ThemeColor</h2>
        <div class="panel-body"><div id="colorGrid" class="color-grid"></div></div>
      </section>

      <section class="panel">
        <h2>ThemeRole</h2>
        <div class="panel-body">
          <div id="roleGrid" class="role-grid"></div>
          <div class="note">A role may reference ThemeColor or use custom HEX. Omitted roles resolve through DEFAULT_THEME_ROLES.</div>
        </div>
      </section>

      <section class="panel">
        <h2>Export JSON</h2>
        <div class="panel-body">
          <textarea id="jsonBox" class="json-box" spellcheck="false"></textarea>
          <div class="small-actions">
            <button id="applyJson" class="secondary-action">Apply JSON</button>
            <button id="copyJson" class="secondary-action">Copy</button>
          </div>
        </div>
      </section>
    </div>

    <div class="preview-wrap">
      <section class="panel">
        <h2>Live controls</h2>
        <div class="panel-body">
          <div id="preview" class="preview">
            <div class="preview-title">ROLE-BASED COMPONENT PREVIEW</div>
            <div class="preview-header">HEADER</div>
            <div class="preview-grid">
              <div class="preview-card">
                <h3>BUTTONS</h3>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
                  <button class="demo-button">BUTTON</button>
                  <button class="demo-button selected">SELECTED</button>
                </div>
              </div>
              <div class="preview-card">
                <h3>TEMPERATURE ROLES</h3>
                <div class="temps">
                  <b class="temp-nozzle">NOZZLE 150C</b>
                  <b class="temp-bed">BED 60C</b>
                  <b class="temp-fan">FAN 80%</b>
                </div>
              </div>
              <div class="preview-card">
                <h3>STATUS COLORS</h3>
                <div class="statuses">
                  <span class="chip warning">WARNING</span>
                  <span class="chip danger">DANGER</span>
                  <span class="chip success">SUCCESS</span>
                </div>
              </div>
              <div class="preview-card">
                <h3>DIRECT BASE TOKENS</h3>
                <div class="tokens">
                  <div class="token t-text"><span>TEXT</span><b>Aa</b></div>
                  <div class="token t-bright"><span>BRIGHT</span><b>Aa</b></div>
                  <div class="token t-primary"><span>PRIMARY</span><b>Aa</b></div>
                  <div class="token t-secondary"><span>SECONDARY</span><b>Aa</b></div>
                  <div class="token t-dim"><span>DIM</span><b>Aa</b></div>
                  <div class="token t-muted"><span>MUTED</span><b>Aa</b></div>
                </div>
              </div>
            </div>
            <div class="runtime-block">
              <h3>RUNTIME COMBINATIONS / REGRESSION CASES</h3>
              <div id="runtimeGrid" class="runtime-grid"></div>
            </div>
          </div>
        </div>
      </section>

      <section class="panel">
        <h2>ThemeRole usage combinations</h2>
        <div class="panel-body">
          <div id="roleSpecimens" class="role-combinations"></div>
          <div class="note">
            Every role is shown once in its runtime combination. Each color is marked DEFAULT or OVERRIDE.
          </div>
        </div>
      </section>

      <section class="panel">
        <h2>Resolved ThemeRole palette</h2>
        <div class="panel-body"><div id="rolePalette" class="palette-grid"></div></div>
      </section>

      <section class="panel">
        <h2>Base ThemeColor palette</h2>
        <div class="panel-body"><div id="basePalette" class="palette-grid"></div></div>
      </section>

      <section class="panel">
        <h2>Physical color map</h2>
        <div class="panel-body">
          <div id="physicalMap" class="physical-grid"></div>
          <div class="note">Unique physical HEX values across both ThemeColor and resolved ThemeRole values.</div>
        </div>
      </section>
    </div>
  </div>
</div>

<dialog id="generatorDialog" class="generator-dialog" aria-labelledby="generatorTitle">
  <div class="generator-head">
    <div>
      <h2 id="generatorTitle">Generate a new theme</h2>
      <div class="sub">
        Choose one color. The harmony rotates its hue while keeping its saturation and lightness.
      </div>
    </div>
    <button id="generatorClose" class="generator-close" aria-label="Close">×</button>
  </div>
  <div class="generator-body">
    <div class="generator-row">
      <div class="field">
        <label for="generatorHex">Base color</label>
        <div class="generator-color">
          <input id="generatorColor" type="color" value="#35d9e6" aria-label="Base color picker">
          <input id="generatorHex" class="hex-input" type="text" value="35d9e6" maxlength="7" aria-label="Base color HEX">
        </div>
      </div>
      <div class="field">
        <label for="generatorHarmony">Color harmony</label>
        <select id="generatorHarmony">
          <option value="triadic">Triadic</option>
          <option value="complementary">Complementary</option>
          <option value="analogous">Analogous</option>
          <option value="split">Split complementary</option>
        </select>
      </div>
    </div>
    <div class="field">
      <label>Appearance</label>
      <div class="generator-mode">
        <label><input type="radio" name="generatorMode" value="dark" checked> Dark</label>
        <label><input type="radio" name="generatorMode" value="light"> Light</label>
      </div>
    </div>
    <div class="field">
      <label>Generated accent palette</label>
      <div id="generatorPalette" class="generator-palette"></div>
    </div>
    <div class="field">
      <label for="generatorName">Theme name</label>
      <input id="generatorName" type="text" maxlength="32" placeholder="LIGHT TEAL">
      <div class="note">You can edit every generated color after creating the theme.</div>
    </div>
  </div>
  <div class="generator-footer">
    <button id="generatorCancel" class="secondary-action">Cancel</button>
    <button id="generatorCreate" class="primary-action">Create Theme</button>
  </div>
</dialog>

<script>
"use strict";

const GENERATED_THEME_STORAGE_KEY = "feather-theme-editor.generated.v1";
const state = {
  config:null,
  doc:null,
  resolvedRoles:{},
  generatedThemes:[],
  currentGeneratedId:null,
  storageWarning:"",
};
const $ = id => document.getElementById(id);

function loadGeneratedThemes() {
  try {
    const stored = JSON.parse(localStorage.getItem(GENERATED_THEME_STORAGE_KEY) || "[]");
    if (!Array.isArray(stored)) return [];

    const seen = new Set();
    return stored.filter(item => {
      const valid = item && typeof item.id === "string" && !seen.has(item.id) &&
        item.doc && typeof item.doc === "object" && item.doc.colors && typeof item.doc.colors === "object" &&
        state.config.colors.every(name => isHex(item.doc.colors[name]));
      if (valid) seen.add(item.id);
      return valid;
    });
  } catch (error) {
    console.warn("Could not load saved generated themes", error);
    state.storageWarning = "saved generated themes could not be read";
    return [];
  }
}

function saveGeneratedThemes() {
  try {
    localStorage.setItem(GENERATED_THEME_STORAGE_KEY, JSON.stringify(state.generatedThemes));
    state.storageWarning = "";
    return true;
  } catch (error) {
    console.warn("Could not save generated themes", error);
    state.storageWarning = "browser storage unavailable; generated themes last only for this session";
    return false;
  }
}

function generatedOptionValue(id) { return `generated:${id}`; }

function renderThemeOptions(selectedValue=null) {
  const select = $("themeSelect");
  select.innerHTML = "";
  for (const theme of state.config.themes) {
    const option = document.createElement("option");
    option.value = theme.file;
    option.textContent = theme.name;
    select.appendChild(option);
  }
  for (const theme of state.generatedThemes) {
    const option = document.createElement("option");
    option.value = generatedOptionValue(theme.id);
    option.textContent = `✦ ${theme.doc.name || "UNTITLED"}`;
    select.appendChild(option);
  }
  if (selectedValue && [...select.options].some(option => option.value === selectedValue)) {
    select.value = selectedValue;
  }
}

function syncDeleteThemeButton() {
  $("deleteTheme").hidden = state.currentGeneratedId === null;
}

function persistCurrentGeneratedTheme() {
  if (state.currentGeneratedId === null) return;
  const stored = state.generatedThemes.find(theme => theme.id === state.currentGeneratedId);
  if (!stored) return;

  stored.doc = exportDocument();
  saveGeneratedThemes();
  const value = generatedOptionValue(stored.id);
  const option = [...$("themeSelect").options].find(item => item.value === value);
  if (option) option.textContent = `✦ ${stored.doc.name || "UNTITLED"}`;
}

function hex(value) { return String(value || "").trim().replace(/^#/, "").toLowerCase(); }
function isHex(value) { return /^[0-9a-f]{6}$/i.test(hex(value)); }
function css(value) { return "#" + hex(value); }
function cssVarName(prefix, name) { return `--${prefix}-${String(name).replaceAll("_", "-")}`; }

function rgbChannels(value) {
  const raw = hex(value);
  return [parseInt(raw.slice(0,2),16), parseInt(raw.slice(2,4),16), parseInt(raw.slice(4,6),16)];
}
function linearChannel(value) {
  value /= 255;
  return value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
}
function luminance(value) {
  const [r,g,b] = rgbChannels(value);
  return 0.2126 * linearChannel(r) + 0.7152 * linearChannel(g) + 0.0722 * linearChannel(b);
}
function contrastRatio(foreground, background) {
  const a = luminance(foreground), b = luminance(background);
  return (Math.max(a,b) + 0.05) / (Math.min(a,b) + 0.05);
}
function diagnosticText(background) {
  return contrastRatio("000000", background) >= contrastRatio("ffffff", background) ? "000000" : "ffffff";
}

function clamp(value, low, high) { return Math.min(high, Math.max(low, value)); }
function wrapHue(value) { return (value % 360 + 360) % 360; }

function rgbToHsl(value) {
  const [rawR,rawG,rawB] = rgbChannels(value), r = rawR / 255, g = rawG / 255, b = rawB / 255;
  const high = Math.max(r,g,b), low = Math.min(r,g,b), delta = high - low;
  let hue = 0;
  if (delta) {
    if (high === r) hue = 60 * (((g - b) / delta) % 6);
    else if (high === g) hue = 60 * ((b - r) / delta + 2);
    else hue = 60 * ((r - g) / delta + 4);
  }
  const lightness = (high + low) / 2;
  const saturation = delta ? delta / (1 - Math.abs(2 * lightness - 1)) : 0;
  return {h:wrapHue(hue), s:saturation * 100, l:lightness * 100};
}

function hslToHex(hue, saturation, lightness) {
  const h = wrapHue(hue), s = clamp(saturation, 0, 100) / 100, l = clamp(lightness, 0, 100) / 100;
  const chroma = (1 - Math.abs(2 * l - 1)) * s;
  const segment = h / 60, middle = chroma * (1 - Math.abs(segment % 2 - 1));
  let rgb = [0,0,0];
  if (segment < 1) rgb = [chroma,middle,0];
  else if (segment < 2) rgb = [middle,chroma,0];
  else if (segment < 3) rgb = [0,chroma,middle];
  else if (segment < 4) rgb = [0,middle,chroma];
  else if (segment < 5) rgb = [middle,0,chroma];
  else rgb = [chroma,0,middle];
  const offset = l - chroma / 2;
  return rgb.map(channel => Math.round((channel + offset) * 255).toString(16).padStart(2,"0")).join("");
}

const HARMONIES = {
  complementary: {label:"Complementary", offsets:[0,180]},
  triadic: {label:"Triadic", offsets:[0,120,240]},
  analogous: {label:"Analogous", offsets:[0,-30,30]},
  split: {label:"Split complementary", offsets:[0,150,210]},
};

function selectedGeneratorMode() {
  return document.querySelector('input[name="generatorMode"]:checked').value;
}

function contrastAdjustedHex(hue, saturation, lightness, background, makeLighter) {
  let value = hslToHex(hue, saturation, lightness);
  for (let step = 0; step < 30 && contrastRatio(value, background) < 4.5; step++) {
    lightness = clamp(lightness + (makeLighter ? 2 : -2), 10, 90);
    value = hslToHex(hue, saturation, lightness);
  }
  return value;
}

function generatedAccentPalette(baseColor, harmony) {
  const base = rgbToHsl(baseColor);
  return HARMONIES[harmony].offsets.map(offset => {
    const hue = wrapHue(base.h + offset);
    return {h:hue, hex:offset === 0 ? hex(baseColor) : hslToHex(hue, base.s, base.l)};
  });
}

function hueName(hue) {
  const h = wrapHue(hue);
  if (h < 15 || h >= 345) return "red";
  if (h < 38) return "orange";
  if (h < 52) return "amber";
  if (h < 70) return "yellow";
  if (h < 100) return "lime";
  if (h < 150) return "green";
  if (h < 180) return "teal";
  if (h < 200) return "cyan";
  if (h < 230) return "blue";
  if (h < 265) return "indigo";
  if (h < 290) return "violet";
  if (h < 325) return "magenta";
  return "rose";
}

function generatorSelection() {
  const base = hex($("generatorColor").value), harmony = $("generatorHarmony").value;
  const mode = selectedGeneratorMode();
  const palette = generatedAccentPalette(base, harmony).map((color,index) => {
    const override = generatorPaletteOverrides[index];
    return override ? {h:rgbToHsl(override).h, hex:override} : color;
  });
  const names = palette.map(color => hueName(color.h));
  return {base,harmony,mode,palette,names};
}

function generatedThemeName(selection) {
  return `${selection.mode}_${selection.names[0]}`.toUpperCase().slice(0,32);
}

function generatedDescription(selection) {
  const list = selection.names.length === 2 ? selection.names.join(" and ") :
    selection.names.slice(0,-1).join(", ") + " and " + selection.names.at(-1);
  const mode = selection.mode[0].toUpperCase() + selection.mode.slice(1);
  return `${mode} ${list} palette (${HARMONIES[selection.harmony].label.toLowerCase()}).`;
}

function shiftedColor(value, lightnessDelta, saturationScale) {
  const color = rgbToHsl(value);
  return hslToHex(color.h, color.s * saturationScale, color.l + lightnessDelta);
}

function generatedColors(selection) {
  const base = rgbToHsl(selection.base);
  const first = selection.palette[0], second = selection.palette[1] || selection.palette[0];
  const primaryDark = shiftedColor(first.hex, -24, .85);
  const secondaryDark = shiftedColor(second.hex, selection.mode === "light" ? 10 : -24, .70);
  if (selection.mode === "light") {
    const panel = hslToHex(base.h, 10, 99);
    return {
      background:hslToHex(base.h, 12, 94), panel,
      primary:first.hex, primary_dark:primaryDark,
      secondary:second.hex, secondary_dark:secondaryDark,
      warning:contrastAdjustedHex(42, 86, 34, panel, false),
      danger:contrastAdjustedHex(355, 72, 39, panel, false),
      danger_background:hslToHex(355, 58, 93), text:diagnosticText(panel),
      bright:diagnosticText(panel), dim:hslToHex(base.h, 9, 40),
      border:hslToHex(base.h, 10, 61), muted:hslToHex(base.h, 9, 70),
      success:contrastAdjustedHex(145, 58, 31, panel, false),
      pressed_background:hslToHex(base.h, 12, 83),
      overlay:hslToHex(base.h, 16, 18),
    };
  }
  const panel = hslToHex(base.h, Math.min(base.s * .30, 25), 9);
  return {
    background:hslToHex(base.h, Math.min(base.s * .28, 24), 4),
    panel,
    primary:first.hex, primary_dark:primaryDark,
    secondary:second.hex, secondary_dark:secondaryDark,
    warning:contrastAdjustedHex(42, 86, 64, panel, true),
    danger:contrastAdjustedHex(355, 76, 64, panel, true),
    danger_background:hslToHex(355, 52, 10), text:diagnosticText(panel),
    bright:diagnosticText(panel), dim:hslToHex(base.h, 12, 48),
    border:hslToHex(base.h, 20, 31), muted:hslToHex(base.h, 16, 18),
    success:contrastAdjustedHex(145, 55, 59, panel, true),
    pressed_background:hslToHex(base.h, 24, 22),
    overlay:hslToHex(base.h, 18, 2),
  };
}

function generatedRoles(selection, colors) {
  const candidates = {
    button_background:"panel", button_border:"border", button_text:"text",
    button_selected_background:"primary_dark", button_selected_border:"primary",
    button_selected_text:diagnosticText(colors.primary_dark),
    accent_background:"secondary_dark", accent_border:"secondary",
    accent_text:diagnosticText(colors.secondary_dark),
    header_background:"primary_dark", header_text:diagnosticText(colors.primary_dark),
    header_border:selection.palette[2]?.hex ?? "primary", temperature_nozzle:"danger",
    temperature_bed:"warning", temperature_fan:"secondary",
  };
  return Object.fromEntries(state.config.roles.map(role => [role, candidates[role] ?? state.config.defaults[role]]));
}

function resolveRoleValue(value) {
  const raw = String(value || "").trim().toLowerCase().replace(/^#/, "");
  return state.doc.colors[raw] || raw;
}
function effectiveRoles() {
  const out = {};
  for (const role of state.config.roles) {
    const source = (state.doc.roles || {})[role] ?? state.config.defaults[role];
    out[role] = resolveRoleValue(source);
  }
  state.resolvedRoles = out;
  return out;
}
function roleSource(role) { return (state.doc.roles || {})[role] ?? state.config.defaults[role]; }
function exportDocument() {
  const copy = JSON.parse(JSON.stringify(state.doc));
  copy.name = $("themeName").value.trim();
  copy.description = $("themeDescription").value.trim();
  return copy;
}

function setCssVariables() {
  const root = document.documentElement;
  for (const name of state.config.colors) root.style.setProperty(cssVarName("c", name), css(state.doc.colors[name]));
  const roles = effectiveRoles();
  for (const name of state.config.roles) root.style.setProperty(cssVarName("r", name), css(roles[name]));
  root.style.setProperty("--diag-background", css(diagnosticText(state.doc.colors.background)));
  root.style.setProperty("--diag-panel", css(diagnosticText(state.doc.colors.panel)));
}

function swatchHtml(name, value, extra="") {
  const raw = hex(value), label = diagnosticText(raw), ratio = contrastRatio(label, raw);
  return `<div class="swatch" style="background:${css(raw)};color:${css(label)}">
    <b>${name}</b><span>#${raw.toUpperCase()}</span>
    <span class="meta">${extra || `label ${ratio.toFixed(2)}:1`}</span>
  </div>`;
}

function renderBasePalette() {
  $("basePalette").innerHTML = state.config.colors.map(name => swatchHtml(name, state.doc.colors[name])).join("");
}
function renderRolePalette() {
  const roles = effectiveRoles();
  $("rolePalette").innerHTML = state.config.roles.map(role => {
    const source = roleSource(role);
    return swatchHtml(role, roles[role], `${source} → #${hex(roles[role]).toUpperCase()}`);
  }).join("");
}
function renderPhysicalMap() {
  const clusters = new Map();
  const add = (value, kind, name) => {
    const raw = hex(value);
    if (!clusters.has(raw)) clusters.set(raw, {colors:[],roles:[]});
    clusters.get(raw)[kind].push(name);
  };
  for (const name of state.config.colors) add(state.doc.colors[name], "colors", name);
  const roles = effectiveRoles();
  for (const role of state.config.roles) add(roles[role], "roles", role);

  const items = [...clusters.entries()].sort((a,b) => {
    const ac = a[1].colors.length + a[1].roles.length;
    const bc = b[1].colors.length + b[1].roles.length;
    return bc - ac || a[0].localeCompare(b[0]);
  });

  $("physicalMap").innerHTML = items.map(([value, info]) => {
    const label = diagnosticText(value);
    return `<div class="physical-item">
      <div class="physical-color" style="background:${css(value)};color:${css(label)}">#${value.toUpperCase()}</div>
      <div class="physical-meta"><b>#${value.toUpperCase()}</b><br>
        ThemeColor: ${info.colors.join(", ") || "—"}<br>
        ThemeRole: ${info.roles.join(", ") || "—"}
      </div>
    </div>`;
  }).join("");
}

function renderRuntimeCombinations() {
  const c = state.doc.colors, r = effectiveRoles();
  const cases = [
    ["INFO / NETWORK", "BRIGHT ON BACKGROUND", c.bright, c.background, c.border, "BRIGHT → BACKGROUND"],
    ["TOAST / ACCENT", "SECONDARY ON BACKGROUND", c.secondary, c.background, c.secondary, "SECONDARY → BACKGROUND"],
    ["CALIBRATION CURRENT", "CURRENT STAGE", c.bright, c.secondary_dark, c.secondary, "BRIGHT / SECONDARY_DARK / SECONDARY border"],
    ["CALIBRATION DONE", "DONE STAGE", c.primary, c.panel, c.primary, "PRIMARY → PANEL"],
    ["FUTURE / DISABLED", "FUTURE STAGE", c.muted, c.panel, c.border, "MUTED → PANEL"],
    ["TOGGLE OFF / AUX", "DIM / MUTED", c.dim, c.panel, c.muted, "DIM text + MUTED structure → PANEL"],
    ["DANGER ACTION", "ABORT / CANCEL", c.danger, c.danger_background, c.danger, "DANGER → DANGER_BACKGROUND"],
    ["SELECTED ROLE SET", "SELECTED", r.button_selected_text, r.button_selected_background, r.button_selected_border, "resolved selected button roles"],
    ["ACCENT ROLE SET", "ACCENT", r.accent_text, r.accent_background, r.accent_border, "resolved accent surface roles"],
  ];

  $("runtimeGrid").innerHTML = cases.map(([title,text,fg,bg,border,detail]) => {
    const ratio = contrastRatio(fg,bg);
    const ratioColor = ratio >= 4.5 ? c.success : ratio >= 3.0 ? c.warning : c.danger;
    return `<div class="runtime-item"><div class="runtime-head"><b>${title}</b>
      <span class="runtime-ratio" style="color:${css(ratioColor)}">${ratio.toFixed(2)}:1</span></div>
      <div class="runtime-sample" style="color:${css(fg)};background:${css(bg)};border-color:${css(border)}">${text}</div>
      <div class="runtime-detail">${detail}</div></div>`;
  }).join("");
}

const ROLE_SPECIMEN_GROUPS = [
  {key:"button", title:"BUTTON", roles:[
    ["BG", "button_background"], ["BORDER", "button_border"], ["TEXT", "button_text"],
  ]},
  {key:"selected", title:"SELECTED BUTTON", roles:[
    ["BG", "button_selected_background"], ["BORDER", "button_selected_border"],
    ["TEXT", "button_selected_text"],
  ]},
  {key:"accent", title:"ACCENT SURFACE", roles:[
    ["BG", "accent_background"], ["BORDER", "accent_border"], ["TEXT", "accent_text"],
  ]},
  {key:"header", title:"HEADER", roles:[
    ["BG", "header_background"], ["BORDER", "header_border"], ["TEXT", "header_text"],
  ]},
  {key:"temperature", title:"TEMPERATURES", roles:[
    ["NOZZLE", "temperature_nozzle"], ["BED", "temperature_bed"], ["FAN", "temperature_fan"],
  ]},
];

function roleCombinationSample(group) {
  if (group.key === "button") return `<button class="role-mini-button"
    style="background:var(--r-button-background);color:var(--r-button-text);
      border:2px solid var(--r-button-border)">BUTTON</button>`;
  if (group.key === "selected") return `<button class="role-mini-button"
    style="background:var(--r-button-selected-background);color:var(--r-button-selected-text);
      border:2px solid var(--r-button-selected-border)">SELECTED</button>`;
  if (group.key === "accent") return `<div class="role-mini-header"
    style="background:var(--r-accent-background);color:var(--r-accent-text);
      border:2px solid var(--r-accent-border)">ACCENT</div>`;
  if (group.key === "header") return `<div class="role-mini-header"
    style="background:var(--r-header-background);color:var(--r-header-text);
      border:2px solid var(--r-header-border)">HEADER</div>`;
  return `<div class="role-mini-temperatures">
    <span style="color:var(--r-temperature-nozzle)">NOZZLE</span>
    <span style="color:var(--r-temperature-bed)">BED</span>
    <span style="color:var(--r-temperature-fan)">FAN</span>
  </div>`;
}

function renderRoleSpecimens() {
  const groupedRoles = ROLE_SPECIMEN_GROUPS.flatMap(group => group.roles.map(item => item[1]));
  const missing = state.config.roles.filter(role => !groupedRoles.includes(role));
  const unknown = groupedRoles.filter(role => !state.config.roles.includes(role));
  if (missing.length || unknown.length || new Set(groupedRoles).size !== groupedRoles.length) {
    throw new Error("ThemeRole specimen groups do not match the theme contract");
  }

  $("roleSpecimens").innerHTML = ROLE_SPECIMEN_GROUPS.map(group => `
    <div class="role-combination" data-combination="${group.key}">
      <div class="role-combination-head"><h3>${group.title}</h3><span>${group.roles.length} COLORS</span></div>
      <div class="role-combination-sample">${roleCombinationSample(group)}</div>
      <div class="role-combination-colors">${group.roles.map(([label, role]) => `
        <div class="role-color-item" data-role="${role}">
          <div class="role-color-head"><b>${label}</b><span class="role-source-badge"></span></div>
          <div class="role-color-value">
            <input class="role-chip-color role-current-picker" type="color" title="Edit current role color">
            <code class="current-label"></code>
          </div>
          <div class="role-color-meta"><code>${role}</code><span class="role-source-label"></span></div>
        </div>`).join("")}</div>
    </div>`).join("");

  for (const item of $("roleSpecimens").querySelectorAll(".role-color-item")) {
    const role = item.dataset.role;
    const currentPicker = item.querySelector(".role-current-picker");

    const applyPicker = picker => {
      state.doc.roles ||= {};
      const value = hex(picker.value);
      state.doc.roles[role] = value;
      syncRoleEditorToCustom(role, value);
      applyLiveState(picker);
    };

    currentPicker.addEventListener("input", () => applyPicker(currentPicker));
    currentPicker.addEventListener("change", () => applyLiveState());
  }
  syncRoleSpecimens();
}

function syncRoleSpecimens(activePicker=null) {
  const roles = effectiveRoles();
  for (const item of $("roleSpecimens").querySelectorAll(".role-color-item")) {
    const role = item.dataset.role;
    const current = roles[role];
    const source = roleSource(role);
    const inherited = (state.doc.roles || {})[role] === undefined;
    const currentPicker = item.querySelector(".role-current-picker");
    const badge = item.querySelector(".role-source-badge");

    badge.textContent = inherited ? "DEFAULT" : "OVERRIDE";
    badge.classList.toggle("override", !inherited);
    item.querySelector(".current-label").textContent = `#${hex(current).toUpperCase()}`;
    item.querySelector(".role-source-label").textContent = `${inherited ? "default" : "source"}: ${source}`;
    if (currentPicker !== activePicker) currentPicker.value = css(current);
  }
}

function renderColorEditor() {
  const root = $("colorGrid"); root.innerHTML = "";
  for (const name of state.config.colors) {
    const value = hex(state.doc.colors[name]);
    const row = document.createElement("div"); row.className = "color-row"; row.dataset.color = name;
    row.innerHTML = `<code>${name}</code><input class="picker" type="color" value="#${value}"><input class="hex-input" type="text" value="${value}" maxlength="7">`;
    const picker = row.querySelector(".picker"), input = row.querySelector(".hex-input");
    picker.addEventListener("input", () => {
      const next = hex(picker.value); state.doc.colors[name] = next; input.value = next; applyLiveState(picker);
    });
    picker.addEventListener("change", () => applyLiveState());
    input.addEventListener("input", () => {
      const next = hex(input.value); if (!isHex(next)) return;
      state.doc.colors[name] = next; picker.value = css(next); applyLiveState(input);
    });
    root.appendChild(row);
  }
}

function roleOptions(selected) {
  const raw = String(selected || "").trim().toLowerCase();
  const isToken = state.config.colors.includes(raw);
  return state.config.colors.map(name => `<option value="${name}" ${raw === name ? "selected" : ""}>${name}</option>`).join("") +
    `<option value="__custom__" ${!isToken ? "selected" : ""}>custom HEX</option>`;
}
function renderRoleEditor() {
  const root = $("roleGrid"); root.innerHTML = "";
  for (const role of state.config.roles) {
    const source = roleSource(role), raw = String(source).trim().toLowerCase(), custom = !state.config.colors.includes(raw);
    const row = document.createElement("div"); row.className = "role-row" + (custom ? " custom" : ""); row.dataset.role = role;
    row.innerHTML = `<code>${role}</code><select>${roleOptions(raw)}</select><input class="role-color" type="color" value="#${isHex(raw) ? hex(raw) : "ffffff"}">`;
    const select = row.querySelector("select"), picker = row.querySelector(".role-color");
    select.addEventListener("change", () => {
      state.doc.roles ||= {};
      if (select.value === "__custom__") {
        row.classList.add("custom"); const value = state.resolvedRoles[role] || "ffffff";
        picker.value = css(value); state.doc.roles[role] = hex(value);
      } else {
        row.classList.remove("custom"); state.doc.roles[role] = select.value;
      }
      applyLiveState();
    });
    picker.addEventListener("input", () => {
      state.doc.roles ||= {}; state.doc.roles[role] = hex(picker.value); applyLiveState(picker);
    });
    picker.addEventListener("change", () => applyLiveState());
    root.appendChild(row);
  }
}

function syncRoleEditorToCustom(role, value) {
  const row = [...document.querySelectorAll(".role-row")].find(item => item.dataset.role === role);
  if (!row) return;
  row.classList.add("custom");
  row.querySelector("select").value = "__custom__";
  row.querySelector(".role-color").value = css(value);
}

function syncColorEditors(active=null) {
  for (const row of document.querySelectorAll(".color-row")) {
    const name = row.dataset.color, value = hex(state.doc.colors[name]);
    const picker = row.querySelector(".picker"), input = row.querySelector(".hex-input");
    if (picker !== active) picker.value = css(value);
    if (input !== active) input.value = value;
  }
}

function updateJsonBox() { $("jsonBox").value = JSON.stringify(exportDocument(), null, 2) + "\n"; }
let validationTimer = null;
function scheduleValidation() { clearTimeout(validationTimer); validationTimer = setTimeout(validateCurrent, 120); }

function applyLiveState(activeControl=null) {
  // One authoritative update path. No role specimen DOM is rebuilt here, so
  // an open native color picker remains open while every dependent view updates.
  effectiveRoles();
  setCssVariables();
  syncColorEditors(activeControl);
  syncRoleSpecimens(activeControl);
  renderRolePalette();
  renderBasePalette();
  renderPhysicalMap();
  renderRuntimeCombinations();
  updateJsonBox();
  persistCurrentGeneratedTheme();
  scheduleValidation();
}

async function validateCurrent() {
  const response = await fetch("/api/validate", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(exportDocument())});
  const result = await response.json(), status = $("validationStatus"), button = $("downloadButton");
  if (result.ok) {
    status.className = "status ok";
    status.textContent = "Valid • " + (result.schema_used ? "schema validation OK" : "basic validation OK");
    button.disabled = false;
  } else {
    status.className = "status bad"; status.textContent = result.errors.join(" • "); button.disabled = true;
  }
  if (state.storageWarning) status.textContent += " • " + state.storageWarning;
}

let generatorNameEdited = false;
let generatorPaletteOverrides = [];

function applyGeneratorAccent(picker) {
  const index = Number(picker.dataset.index);
  generatorPaletteOverrides[index] = hex(picker.value);
  const selection = generatorSelection(), color = selection.palette[index];
  const swatch = picker.closest(".generator-swatch"), label = diagnosticText(color.hex);
  swatch.style.background = css(color.hex);
  swatch.style.color = css(label);
  swatch.querySelector("b").textContent = selection.names[index];
  swatch.querySelector(".hex-label").textContent = `#${color.hex.toUpperCase()}`;
  picker.setAttribute("aria-label", `Adjust ${selection.names[index]} accent`);
  if (!generatorNameEdited) $("generatorName").value = generatedThemeName(selection);
}

function refreshGeneratorPreview(resetPalette=false) {
  if (resetPalette) generatorPaletteOverrides = [];
  const selection = generatorSelection(), root = $("generatorPalette");
  root.style.gridTemplateColumns = `repeat(${selection.palette.length},1fr)`;
  root.innerHTML = selection.palette.map((color,index) => {
    const label = diagnosticText(color.hex);
    return `<label class="generator-swatch" style="background:${css(color.hex)};color:${css(label)}">
      <input type="color" value="${css(color.hex)}" data-index="${index}"
        aria-label="Adjust ${selection.names[index]} accent">
      <b>${selection.names[index]}</b><span class="hex-label">#${color.hex.toUpperCase()}</span>
      <span class="edit-hint">click to adjust</span></label>`;
  }).join("");
  for (const picker of root.querySelectorAll('input[type="color"]')) {
    picker.addEventListener("input", () => applyGeneratorAccent(picker));
  }
  if (!generatorNameEdited) $("generatorName").value = generatedThemeName(selection);
}

function openGenerator() {
  if (!state.doc) return;
  const primary = state.doc.colors.primary;
  if (isHex(primary)) {
    $("generatorColor").value = css(primary);
    $("generatorHex").value = hex(primary);
  }
  const mode = luminance(state.doc.colors.background) > .45 ? "light" : "dark";
  document.querySelector(`input[name="generatorMode"][value="${mode}"]`).checked = true;
  $("generatorHarmony").value = "triadic";
  generatorNameEdited = false;
  refreshGeneratorPreview(true);
  $("generatorDialog").showModal();
}

function normalizeGeneratedName(value, fallback) {
  let name = String(value || "").trim().toUpperCase().replace(/[^A-Z0-9]+/g,"_").replace(/^_+|_+$/g,"");
  if (!name) name = fallback;
  if (!/^[A-Z]/.test(name)) name = "THEME_" + name;
  return name.slice(0,32).replace(/_+$/g,"");
}

function newGeneratedThemeId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2,8)}`;
}

function showCurrentDocument(name, description) {
  state.doc.roles ||= {};
  $("themeName").value = name;
  $("themeDescription").value = description;
  syncDeleteThemeButton();
  renderColorEditor();
  renderRoleEditor();
  renderRoleSpecimens();
  applyLiveState();
}

function createGeneratedTheme() {
  const selection = generatorSelection(), fallbackName = generatedThemeName(selection);
  const next = JSON.parse(JSON.stringify(exportDocument()));
  next.name = normalizeGeneratedName($("generatorName").value, fallbackName);
  next.description = generatedDescription(selection);
  const colors = generatedColors(selection);
  next.colors = {...next.colors, ...colors};
  next.roles = generatedRoles(selection, next.colors);

  state.doc = next;
  state.currentGeneratedId = newGeneratedThemeId();
  state.generatedThemes.push({id:state.currentGeneratedId, doc:JSON.parse(JSON.stringify(next))});
  saveGeneratedThemes();
  renderThemeOptions(generatedOptionValue(state.currentGeneratedId));
  showCurrentDocument(next.name, next.description);
  $("generatorDialog").close();
}

async function loadTheme(filename) {
  state.currentGeneratedId = null;
  const response = await fetch("/api/theme?file=" + encodeURIComponent(filename));
  state.doc = await response.json();
  const name = (state.doc.name || filename.replace(/\.json$/i, "")) + " CUSTOM";
  showCurrentDocument(name, state.doc.description || "");
}

function loadGeneratedTheme(id) {
  const stored = state.generatedThemes.find(theme => theme.id === id);
  if (!stored) return;

  state.currentGeneratedId = id;
  state.doc = JSON.parse(JSON.stringify(stored.doc));
  showCurrentDocument(state.doc.name || "", state.doc.description || "");
}

async function deleteCurrentGeneratedTheme() {
  const id = state.currentGeneratedId;
  const stored = state.generatedThemes.find(theme => theme.id === id);
  if (!stored || !confirm(`Delete saved theme "${stored.doc.name || "UNTITLED"}"?`)) return;

  state.generatedThemes = state.generatedThemes.filter(theme => theme.id !== id);
  state.currentGeneratedId = null;
  saveGeneratedThemes();
  renderThemeOptions();
  syncDeleteThemeButton();

  if (state.config.themes.length) {
    $("themeSelect").value = state.config.themes[0].file;
    await loadTheme(state.config.themes[0].file);
  } else if (state.generatedThemes.length) {
    const next = state.generatedThemes[0];
    $("themeSelect").value = generatedOptionValue(next.id);
    loadGeneratedTheme(next.id);
  } else {
    $("validationStatus").className = "status bad";
    $("validationStatus").textContent = "No themes available.";
    $("downloadButton").disabled = true;
  }
}

async function init() {
  state.config = await (await fetch("/api/config")).json();
  state.generatedThemes = loadGeneratedThemes();
  renderThemeOptions();
  const select = $("themeSelect");
  select.addEventListener("change", () => {
    if (select.value.startsWith("generated:")) loadGeneratedTheme(select.value.slice("generated:".length));
    else loadTheme(select.value);
  });
  $("themeName").addEventListener("input", () => {
    updateJsonBox(); persistCurrentGeneratedTheme(); scheduleValidation();
  });
  $("themeDescription").addEventListener("input", () => {
    updateJsonBox(); persistCurrentGeneratedTheme(); scheduleValidation();
  });
  $("deleteTheme").addEventListener("click", deleteCurrentGeneratedTheme);
  $("generateButton").addEventListener("click", openGenerator);
  $("generatorClose").addEventListener("click", () => $("generatorDialog").close());
  $("generatorCancel").addEventListener("click", () => $("generatorDialog").close());
  $("generatorCreate").addEventListener("click", createGeneratedTheme);
  $("generatorColor").addEventListener("input", () => {
    $("generatorHex").value = hex($("generatorColor").value);
    refreshGeneratorPreview(true);
  });
  $("generatorHex").addEventListener("input", () => {
    const value = hex($("generatorHex").value);
    if (!isHex(value)) return;
    $("generatorColor").value = css(value);
    refreshGeneratorPreview(true);
  });
  $("generatorHex").addEventListener("blur", () => {
    $("generatorHex").value = hex($("generatorColor").value);
  });
  $("generatorHarmony").addEventListener("change", () => refreshGeneratorPreview(true));
  for (const radio of document.querySelectorAll('input[name="generatorMode"]')) {
    radio.addEventListener("change", () => refreshGeneratorPreview(true));
  }
  $("generatorName").addEventListener("input", () => { generatorNameEdited = true; });
  $("downloadButton").addEventListener("click", async () => {
    const response = await fetch("/api/export", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(exportDocument())});
    if (!response.ok) { const result = await response.json(); alert(result.errors.join("\n")); return; }
    const blob = await response.blob(), disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/), filename = match ? match[1] : "custom-theme.json";
    const url = URL.createObjectURL(blob), link = document.createElement("a"); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $("applyJson").addEventListener("click", () => {
    try {
      state.doc = JSON.parse($("jsonBox").value); state.doc.roles ||= {};
      $("themeName").value = state.doc.name || ""; $("themeDescription").value = state.doc.description || "";
      renderColorEditor(); renderRoleEditor(); renderRoleSpecimens(); applyLiveState();
    } catch (error) { alert(error.message); }
  });
  $("copyJson").addEventListener("click", async () => navigator.clipboard.writeText($("jsonBox").value));
  if (state.config.themes.length) {
    select.value = state.config.themes[0].file;
    await loadTheme(select.value);
  } else if (state.generatedThemes.length) {
    select.value = generatedOptionValue(state.generatedThemes[0].id);
    loadGeneratedTheme(state.generatedThemes[0].id);
  } else {
    $("validationStatus").className = "status bad";
    $("validationStatus").textContent = "No theme JSON files found.";
    $("downloadButton").disabled = true;
  }
}
init();
</script>
</body>
</html>
'''


class App:
    def __init__(self, themes_dir, theme_py, schema):
        self.themes_dir = Path(themes_dir)
        self.theme_py = Path(theme_py) if theme_py else None
        self.schema = Path(schema) if schema else None
        self.contract = load_contract(self.theme_py)

    def theme_list(self):
        return load_theme_files(self.themes_dir)

    def safe_theme_path(self, filename):
        filename = Path(str(filename)).name
        path = (self.themes_dir / filename).resolve()
        if path.parent != self.themes_dir.resolve():
            raise ValueError("invalid theme path")
        if not path.is_file():
            raise FileNotFoundError(filename)
        return path

    def load_theme(self, filename):
        document = json.loads(self.safe_theme_path(filename).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or "colors" not in document:
            raise ValueError("not a theme document")
        return document

    def validate(self, document):
        errors = validate_basic(document, self.contract)
        schema_used = False
        if not errors:
            schema_errors, schema_used = schema_validate(document, self.schema)
            errors.extend(schema_errors)
        return errors, schema_used


def make_handler(app, log_requests=True):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FeatherThemeEditor/1.2"

        def log_message(self, fmt, *args):
            if log_requests:
                print("[http] " + fmt % args)

        def send_bytes(self, data, content_type, status=HTTPStatus.OK, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, value, status=HTTPStatus.OK, headers=None):
            data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_bytes(data, "application/json; charset=utf-8", status, headers)

        def read_json(self):
            length = int(self.headers.get("Content-Length") or "0")
            if length > 2_000_000:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self.send_bytes(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/config":
                self.send_json({
                    "themes": app.theme_list(),
                    "colors": list(app.contract["colors"]),
                    "roles": list(app.contract["roles"]),
                    "defaults": app.contract["defaults"],
                    "contract_mode": app.contract["mode"],
                    "schema": app.schema.name if app.schema else None,
                })
                return
            if parsed.path == "/api/theme":
                filename = (urllib.parse.parse_qs(parsed.query).get("file") or [""])[0]
                try:
                    self.send_json(app.load_theme(filename))
                except FileNotFoundError:
                    self.send_json({"error": "theme not found"}, HTTPStatus.NOT_FOUND)
                except Exception as exc:
                    self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            try:
                document = self.read_json()
            except Exception as exc:
                self.send_json({"ok": False, "errors": [str(exc)]}, HTTPStatus.BAD_REQUEST)
                return

            if self.path == "/api/validate":
                errors, schema_used = app.validate(document)
                self.send_json({"ok": not errors, "errors": errors, "schema_used": schema_used})
                return

            if self.path == "/api/export":
                errors, _schema_used = app.validate(document)
                if errors:
                    self.send_json({"ok": False, "errors": errors}, HTTPStatus.BAD_REQUEST)
                    return
                filename = safe_filename(document.get("name")) + ".json"
                data = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                self.send_bytes(
                    data,
                    "application/json; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % filename},
                )
                return

            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--themes-dir", type=Path,
        help="theme directory; defaults to cwd when cwd contains themes, otherwise ./themes")
    parser.add_argument("--theme-py", type=Path, help="path to theme.py; auto-detected when omitted")
    parser.add_argument("--schema", type=Path, help="path to theme.schema.json; auto-detected when omitted")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument(
        "--url-host", help="host or IP printed in the editor URL; defaults to --host")
    parser.add_argument("--port", type=int, default=8765, help="local HTTP port, or 0 for any free port")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    parser.add_argument("--quiet-http", action="store_true", help="do not print HTTP request logs")
    return parser.parse_args()


def main():
    args = parse_args()
    base = Path.cwd().resolve()
    themes_dir = discover_themes_dir(base, args.themes_dir)
    theme_py = args.theme_py.expanduser().resolve() if args.theme_py else discover_theme_py(base)
    schema = args.schema.expanduser().resolve() if args.schema else discover_schema(base, themes_dir)
    public_host = args.url_host or args.host

    app = App(themes_dir, theme_py, schema)
    themes = app.theme_list()
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(app, not args.quiet_http))
    _bind_host, port = server.server_address
    url = "http://%s:%d/" % (public_host, port)

    print("Feather Theme Editor")
    print("  themes   : %s (%d found)" % (themes_dir, len(themes)))
    print("  theme.py : %s" % (theme_py or "not found; embedded contract"))
    print("  schema   : %s" % (schema or "not found; basic validation only"))
    print("  url      : %s" % url)
    print("Press Ctrl+C to stop.", flush=True)

    if not args.no_open:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
