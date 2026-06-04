# mcp_file_tools.py
import functools
from typing import Any, Optional
from mcp.server.fastmcp import FastMCP
from pydantic import create_model, Field

# Import your existing tool registry from the merged single file.
from file_tools import TOOL_MAP, TOOLS

# FastMCP provides the .tool() decorator and handles stdio transport automatically
mcp = FastMCP("file-tools-mcp")

ALL_TOOLS = TOOLS
ALL_TOOL_MAP = TOOL_MAP

def _normalize_tool_schema(schema: dict) -> dict:
    """Normalize tool schemas from either nested or OpenAI-style formats."""
    if "function" in schema and isinstance(schema["function"], dict):
        return schema["function"]
    if schema.get("type") == "function" and "name" in schema and "parameters" in schema:
        return schema
    if "name" in schema and "parameters" in schema:
        return schema
    raise KeyError("Invalid tool schema: missing 'function' or OpenAI-style schema keys")


def register_mcp_tool(schema: dict, impl: callable):
    """Dynamically register a tool with MCP using the provided JSON schema."""
    normalized = _normalize_tool_schema(schema)
    func_name = normalized["name"]
    description = normalized.get("description", "")
    params_schema = normalized["parameters"]
    props = params_schema.get("properties", {})
    required = params_schema.get("required", [])

    # Build field definitions compatible with Pydantic v2
    fields = {}
    for name, spec in props.items():
        py_type = str
        spec_type = spec.get("type")
        if spec_type == "integer":
            py_type = int
        elif spec_type == "boolean":
            py_type = bool
        elif spec_type == "number":
            py_type = float
        elif spec_type == "array":
            item_type = spec.get("items", {}).get("type")
            if item_type == "string":
                py_type = list[str]
            elif item_type == "integer":
                py_type = list[int]
            elif item_type == "number":
                py_type = list[float]
            elif item_type == "boolean":
                py_type = list[bool]
            else:
                py_type = list[Any]
        elif spec_type == "object":
            py_type = dict[str, Any]

        # For required fields, use an ellipsis default so Pydantic marks them required.
        if name in required:
            fields[name] = (py_type, ...)
        else:
            default = spec.get("default", None)
            fields[name] = (Optional[py_type], Field(default=default, description=spec.get("description", "")))

    # Create dynamic Pydantic model
    Params = create_model(f"{func_name}Params", **fields)

    # Register the tool using a short machine-friendly name (func_name).
    # Passing the long description as the decorator argument caused MCP to
    # treat the description as the tool name, triggering validation warnings.
    @mcp.tool(func_name)
    @functools.wraps(impl)
    def wrapper(**kwargs: Any) -> dict:
        try:
            validated = Params(**kwargs)
            # Convert to dict, dropping None values to match original tool signatures
            args = validated.model_dump(exclude_none=True)
            return impl(**args)
        except Exception as e:
            # Match your existing error contract
            return {"ok": False, "error": f"Tool execution failed: {str(e)}"}

    # Attach the human-readable description to the wrapper for tooling
    wrapper.__doc__ = description
    wrapper.__name__ = func_name
    return wrapper

# Register all tools dynamically
for tool_schema in ALL_TOOLS:
    try:
        normalized = _normalize_tool_schema(tool_schema)
        func_name = normalized["name"]
    except KeyError as exc:
        print(f"Skipping invalid tool schema: {exc}")
        continue

    impl = ALL_TOOL_MAP.get(func_name)
    if impl:
        register_mcp_tool(tool_schema, impl)

if __name__ == "__main__":
    print("- Starting MCP File Tools Server...")
    print("- Registered tools:", list(ALL_TOOL_MAP.keys()))
    print("- Transport: stdio (ready for LM Studio, Cursor, Claude, etc.)")
    mcp.run(transport="stdio")
