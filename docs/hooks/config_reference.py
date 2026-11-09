"""MkDocs hook: render the configuration reference from the JSON Schema.

Any page containing ``<!-- config-reference -->`` gets a section per model of
the simulation file format, generated from :class:`fieldsim.SimulationConfig`,
so the reference can never drift from the code.
"""

from __future__ import annotations

import json
from typing import Any

MARKER = "<!-- config-reference -->"

# Order in which models are documented; anything else follows alphabetically.
ORDER = [
    "SimulationConfig",
    "ModbusDeviceSpec",
    "RegisterMap",
    "CoilPoint",
    "DiscreteInputPoint",
    "HoldingRegisterPoint",
    "InputRegisterPoint",
    "OpcUaDeviceSpec",
    "OpcUaFolder",
    "OpcUaVariable",
    "S7DeviceSpec",
    "S7DataBlock",
    "S7Area",
    "S7Point",
    "ScenarioStep",
]


def _type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return f"[{name}](#{name.lower()})"
    if "const" in schema:
        return f"`{json.dumps(schema['const'])}`"
    if "enum" in schema:
        return " \\| ".join(f"`{json.dumps(v)}`" for v in schema["enum"])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = [_type(s) for s in schema[key] if s.get("type") != "null"]
            optional = any(s.get("type") == "null" for s in schema[key])
            text = " \\| ".join(dict.fromkeys(parts))
            return f"{text} (optional)" if optional else text
    kind = schema.get("type")
    if kind == "array":
        return f"list of {_type(schema.get('items', {}))}"
    if kind == "object":
        extra = schema.get("additionalProperties")
        return f"mapping of {_type(extra)}" if isinstance(extra, dict) else "mapping"
    if "pattern" in schema and kind == "string":
        return "string"
    return f"{kind}" if kind else "any"


def _default(field: dict[str, Any], required: bool) -> str:
    if required:
        return "**required**"
    if "default" in field:
        return f"`{json.dumps(field['default'])}`"
    return ""


def _constraints(field: dict[str, Any]) -> str:
    parts = []
    for key, label in (("minimum", "≥"), ("maximum", "≤"), ("exclusiveMinimum", ">")):
        if key in field:
            parts.append(f"{label} {field[key]}")
    if "minItems" in field:
        parts.append(f"at least {field['minItems']} item(s)")
    return f" ({', '.join(parts)})" if parts else ""


def _model_section(name: str, model: dict[str, Any]) -> str:
    lines = [f"### {name}", ""]
    if model.get("description"):
        lines += [model["description"].strip(), ""]
    properties = model.get("properties", {})
    if not properties:
        return "\n".join(lines)
    required = set(model.get("required", []))
    lines += ["| Key | Type | Default | Description |", "|---|---|---|---|"]
    for key, field in properties.items():
        description = (field.get("description") or "").replace("\n", " ")
        lines.append(
            f"| `{key}` | {_type(field)}{_constraints(field)} | "
            f"{_default(field, key in required)} | {description} |"
        )
    return "\n".join(lines) + "\n"


def render() -> str:
    from fieldsim import SimulationConfig

    schema = SimulationConfig.model_json_schema()
    models = dict(schema.get("$defs", {}))
    models["SimulationConfig"] = {k: v for k, v in schema.items() if k != "$defs"}
    names = [n for n in ORDER if n in models] + sorted(n for n in models if n not in ORDER)
    return "\n".join(_model_section(name, models[name]) for name in names)


def on_page_markdown(markdown: str, **_: Any) -> str:
    if MARKER in markdown:
        return markdown.replace(MARKER, render())
    return markdown
