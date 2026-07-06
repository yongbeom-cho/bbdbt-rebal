const FIELD_GROUPS = [
  {
    title: "Regime (bull/bear)",
    fields: [
      { key: "regime_ma_type", label: "MA 종류", type: "select" },
      { key: "d_interval", label: "d_interval", type: "int" },
      { key: "period", label: "period", type: "int" },
    ],
  },
  {
    title: "Bear drop",
    fields: [
      { key: "bear_take_profit_pct", label: "bear_take_profit_pct", type: "float" },
      { key: "bear_day_drop_buy_pct", label: "bear_day_drop_buy_pct", type: "float" },
      { key: "bear_equity_buy_frac", label: "bear_equity_buy_frac", type: "float" },
      {
        key: "bear_day_surge_partial_exit_pct",
        label: "bear_day_surge_partial_exit_pct",
        type: "float",
      },
      { key: "bear_day_surge_sell_newest_n", label: "bear_day_surge_sell_newest_n", type: "int" },
    ],
  },
  {
    title: "Bull drop",
    fields: [
      { key: "bull_take_profit_pct", label: "bull_take_profit_pct", type: "float" },
      { key: "bull_day_drop_buy_pct", label: "bull_day_drop_buy_pct", type: "float" },
      { key: "bull_equity_buy_frac", label: "bull_equity_buy_frac", type: "float" },
      {
        key: "bull_day_surge_partial_exit_pct",
        label: "bull_day_surge_partial_exit_pct",
        type: "float",
      },
      { key: "bull_day_surge_sell_newest_n", label: "bull_day_surge_sell_newest_n", type: "int" },
    ],
  },
  {
    title: "Costs",
    fields: [
      { key: "commission", label: "commission", type: "float" },
      { key: "slippage", label: "slippage", type: "float" },
    ],
  },
];

function axisOptions(axes, key) {
  const v = axes?.[key];
  if (!Array.isArray(v)) return null;
  return v;
}

function FieldControl({ field, value, axes, onChange }) {
  const opts = axisOptions(axes, field.key);
  const listId = `axis-${field.key}`;

  if (field.type === "select" || field.key === "regime_ma_type") {
    const base = opts ?? ["sma", "wma", "ema"];
    const choices =
      value != null && value !== "" && !base.includes(value)
        ? [...base, value]
        : base;
    return (
      <select value={value ?? ""} onChange={(e) => onChange(field.key, e.target.value)}>
        {choices.map((c) => (
          <option key={String(c)} value={c}>
            {String(c)}
          </option>
        ))}
      </select>
    );
  }

  const step = field.type === "int" ? 1 : field.key.includes("pct") ? 0.0001 : 0.01;
  return (
    <div className="field-input-wrap">
      <input
        type="number"
        step={step}
        list={opts?.length ? listId : undefined}
        value={value ?? ""}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === "") return;
          const n = field.type === "int" ? parseInt(raw, 10) : parseFloat(raw);
          if (!Number.isNaN(n)) onChange(field.key, n);
        }}
      />
      {opts?.length > 0 && (
        <datalist id={listId}>
          {opts.map((c) => (
            <option key={String(c)} value={String(c)} />
          ))}
        </datalist>
      )}
    </div>
  );
}

export default function StrategyParamsForm({
  values,
  axes,
  schemaSource,
  viewerDefaultSource,
  onChange,
  onPreset,
}) {
  return (
    <div className="strategy-params">
      {viewerDefaultSource && (
        <p className="hint schema-hint">기본값: {viewerDefaultSource}</p>
      )}
      {schemaSource && (
        <p className="hint schema-hint">
          축: {schemaSource} · 숫자 필드는 직접 입력 가능 (grid 제안은 입력 시 표시)
        </p>
      )}
      <div className="param-presets">
        <button type="button" className="link-btn" onClick={() => onPreset("default")}>
          auto_trader 기본 (config14.json)
        </button>
        <button type="button" className="link-btn" onClick={() => onPreset("mid")}>
          grid 중간값
        </button>
        <button type="button" className="link-btn" onClick={() => onPreset("first")}>
          grid 첫값
        </button>
      </div>
      {FIELD_GROUPS.map((g) => (
        <details key={g.title} className="param-group" open={g.title.startsWith("Regime")}>
          <summary>{g.title}</summary>
          {g.fields.map((f) => (
            <div key={f.key} className="field field-compact">
              <label title={f.key}>{f.label}</label>
              <FieldControl
                field={f}
                value={values[f.key]}
                axes={axes}
                onChange={onChange}
              />
            </div>
          ))}
        </details>
      ))}
    </div>
  );
}
