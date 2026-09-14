"""Ad-hoc check that the tool registry builds and every tool exports a sane schema."""

from __future__ import annotations

from munshiji.agent.registry import build_registry


def main() -> None:
    registry = build_registry()
    print(f"tools: {len(registry)}")
    for tool in registry:
        spec = tool.spec()
        props = sorted(spec.parameters.get("properties", {}))
        required = spec.parameters.get("required", [])
        kind = "WRITE" if tool.requires_approval else "read "
        print(f"  {kind} {tool.name:<26} params={props} required={required}")
    print("OK")


if __name__ == "__main__":
    main()
