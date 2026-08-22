/*
 * Feather Theme Editor browser application.
 *
 * Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
 *
 * This file may be distributed under the terms of the GNU GPLv3 license
 */

"use strict";

const GENERATED_THEME_STORAGE_KEY = "feather-theme-editor.generated.v1";
const state = {
  config:null,
  doc:null,
  resolvedRoles:{},
  generatedThemes:[],
  currentGeneratedId:null,
  applyIdentity:null,
  runtime:{printer:false,can_apply:false,reason:"Checking printer…"},
  documentValid:false,
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
  stored.applyIdentity = state.applyIdentity;
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

function syncSaveActions() {
  $("downloadButton").disabled = !state.documentValid;
  $("applyButton").disabled = !state.documentValid || !state.runtime.can_apply;
  $("applyHint").textContent = state.runtime.can_apply ?
    "Saves the theme on this printer and activates it." : state.runtime.reason;
}

async function refreshRuntime() {
  try {
    const response = await fetch("/api/runtime");
    state.runtime = await response.json();
  } catch (error) {
    state.runtime = {printer:false,can_apply:false,reason:`Printer check failed: ${error.message}`};
  }
  syncSaveActions();
  return state.runtime;
}

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
  const result = await response.json(), status = $("validationStatus");
  state.documentValid = result.ok;
  if (result.ok) {
    status.className = "status ok";
    status.textContent = "Valid • " + (result.schema_used ? "schema validation OK" : "basic validation OK");
  } else {
    status.className = "status bad"; status.textContent = result.errors.join(" • ");
  }
  if (state.storageWarning) status.textContent += " • " + state.storageWarning;
  syncSaveActions();
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
  state.applyIdentity = `generated:${state.currentGeneratedId}`;
  state.generatedThemes.push({
    id:state.currentGeneratedId,
    applyIdentity:state.applyIdentity,
    doc:JSON.parse(JSON.stringify(next)),
  });
  saveGeneratedThemes();
  renderThemeOptions(generatedOptionValue(state.currentGeneratedId));
  showCurrentDocument(next.name, next.description);
  $("generatorDialog").close();
}

async function loadTheme(filename) {
  state.currentGeneratedId = null;
  state.applyIdentity = `bundled:${filename}`;
  const response = await fetch("/api/theme?file=" + encodeURIComponent(filename));
  state.doc = await response.json();
  const name = (state.doc.name || filename.replace(/\.json$/i, "")) + " CUSTOM";
  showCurrentDocument(name, state.doc.description || "");
}

function loadGeneratedTheme(id) {
  const stored = state.generatedThemes.find(theme => theme.id === id);
  if (!stored) return;

  state.currentGeneratedId = id;
  state.applyIdentity = stored.applyIdentity || `generated:${id}`;
  stored.applyIdentity = state.applyIdentity;
  state.doc = JSON.parse(JSON.stringify(stored.doc));
  showCurrentDocument(state.doc.name || "", state.doc.description || "");
}

async function deleteCurrentGeneratedTheme() {
  const id = state.currentGeneratedId;
  const stored = state.generatedThemes.find(theme => theme.id === id);
  if (!stored || !confirm(`Delete saved theme "${stored.doc.name || "UNTITLED"}"?`)) return;

  state.generatedThemes = state.generatedThemes.filter(theme => theme.id !== id);
  state.currentGeneratedId = null;
  state.applyIdentity = null;
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
    state.documentValid = false;
    syncSaveActions();
  }
}

async function downloadCurrentTheme() {
  $("saveMenu").open = false;
  const response = await fetch("/api/export", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(exportDocument())});
  if (!response.ok) { const result = await response.json(); alert(result.errors.join("\n")); return; }
  const blob = await response.blob(), disposition = response.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/), filename = match ? match[1] : "custom-theme.json";
  const url = URL.createObjectURL(blob), link = document.createElement("a"); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function applyCurrentTheme() {
  $("saveMenu").open = false;
  const runtime = await refreshRuntime(), status = $("validationStatus");
  if (!runtime.can_apply) {
    status.className = "status bad";
    status.textContent = runtime.reason;
    return;
  }

  status.className = "status";
  status.textContent = "Saving and applying theme…";
  const response = await fetch("/api/apply", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({identity:state.applyIdentity,document:exportDocument()}),
  });
  const result = await response.json();
  if (!response.ok) {
    status.className = "status bad";
    status.textContent = (result.errors || ["Theme could not be applied."]).join(" • ");
    await refreshRuntime();
    return;
  }

  status.className = "status ok";
  status.textContent = `Applied ${result.name} • ${result.filename}`;
  persistCurrentGeneratedTheme();
}

async function init() {
  state.config = await (await fetch("/api/config")).json();
  await refreshRuntime();
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
  $("saveMenu").addEventListener("toggle", () => {
    if ($("saveMenu").open) refreshRuntime();
  });
  $("downloadButton").addEventListener("click", downloadCurrentTheme);
  $("applyButton").addEventListener("click", applyCurrentTheme);
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
    state.documentValid = false;
    syncSaveActions();
  }
}
init();
