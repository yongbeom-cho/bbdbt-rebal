/** Presets from /api/grid-schema (auto_trader/config14.json, grid mid/first). */

export function gridDefaultsViewer(schema) {
  return { ...(schema?.defaults || {}) };
}

export function gridDefaultsMid(schema) {
  return { ...(schema?.defaults_mid || {}) };
}

export function gridDefaultsFirst(schema) {
  return { ...(schema?.defaults_first || {}) };
}

export function applyGridPreset(schema, prev, name) {
  const base = { ...(prev || {}) };
  const patch =
    name === "first"
      ? gridDefaultsFirst(schema)
      : name === "mid"
        ? gridDefaultsMid(schema)
        : gridDefaultsViewer(schema);
  return { ...base, ...patch };
}
