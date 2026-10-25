"""A small tool runtime: schemas, validation, workspace boundaries, errors."""
from __future__ import annotations

import ast
import operator
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

from .types import ToolCall, ToolResult


class ValidationError(ValueError):
    """Raised when tool arguments do not match the declared schema."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_args(args: Any, schema: Dict[str, Any], path: str = "") -> None:
    """Validate a small, useful subset of JSON Schema.

    This is deliberately not a full JSON Schema implementation.  It covers the
    constructs used by the tutorial: type, required, enum, additionalProperties,
    array items, min/max, and string length.
    """
    expected = schema.get("type")
    if expected:
        python_type = TYPE_MAP.get(expected)
        if python_type is None:
            raise ValidationError(f"unsupported schema type: {expected}", path)
        # bool is a subclass of int in Python; reject it for integer/number.
        if expected in {"integer", "number"} and isinstance(args, bool):
            raise ValidationError(f"{path or 'value'} must be {expected}", path)
        if not isinstance(args, python_type):
            raise ValidationError(
                f"{path or 'value'} must be {expected}, got {type(args).__name__}",
                path,
            )

    if "enum" in schema and args not in schema["enum"]:
        raise ValidationError(
            f"{path or 'value'} must be one of {schema['enum']}", path
        )

    if isinstance(args, str):
        if "minLength" in schema and len(args) < schema["minLength"]:
            raise ValidationError(f"{path} is too short", path)
        if "maxLength" in schema and len(args) > schema["maxLength"]:
            raise ValidationError(f"{path} is too long", path)

    if isinstance(args, (int, float)) and not isinstance(args, bool):
        if "minimum" in schema and args < schema["minimum"]:
            raise ValidationError(f"{path} must be >= {schema['minimum']}", path)
        if "maximum" in schema and args > schema["maximum"]:
            raise ValidationError(f"{path} must be <= {schema['maximum']}", path)

    if isinstance(args, dict):
        props = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in args:
                raise ValidationError(f"missing required field: {key}", key)
        if schema.get("additionalProperties") is False:
            extra = set(args) - set(props)
            if extra:
                raise ValidationError(
                    f"unexpected fields: {sorted(extra)}", ",".join(sorted(extra))
                )
        for key, value in args.items():
            if key in props:
                validate_args(value, props[key], f"{path}.{key}" if path else key)

    if isinstance(args, list) and "items" in schema:
        for index, item in enumerate(args):
            validate_args(item, schema["items"], f"{path}[{index}]")


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Any]
    side_effect: str = "read"  # read | write | destructive
    timeout_s: float = 10.0

    def spec(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Workspace:
    """All file tools are confined to this directory."""

    root: Path
    max_read_bytes: int = 20_000
    max_write_bytes: int = 100_000

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, raw: str) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise ValidationError("path must be a non-empty string", "path")
        if "\x00" in raw:
            raise ValidationError("path contains NUL byte", "path")
        candidate = (self.root / raw).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValidationError("path escapes the workspace", "path")
        return candidate


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------


_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent is too large")
        result = _ALLOWED_BINOPS[type(node.op)](left, right)
        if isinstance(result, float) and (result != result or abs(result) == float("inf")):
            raise ValueError("result is not finite")
        if abs(result) > 10**100:
            raise ValueError("result is too large")
        return result
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_eval_node(node.operand))
    raise ValueError("unsupported expression")


def calculator(args: Dict[str, Any]) -> Dict[str, Any]:
    expression = str(args["expression"]).strip()
    if len(expression) > 200:
        raise ValueError("expression is too long")
    tree = ast.parse(expression, mode="eval")
    value = _eval_node(tree)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return {"expression": expression, "result": value}


def make_file_tools(workspace: Workspace) -> List[Tool]:
    def read_file(args: Dict[str, Any]) -> Dict[str, Any]:
        path = workspace.resolve(args["path"])
        if not path.is_file():
            raise FileNotFoundError(f"not a file: {args['path']}")
        size = path.stat().st_size
        # Read at most one byte past the limit so a huge file cannot exhaust
        # memory just to be truncated afterwards.
        with path.open("rb") as handle:
            raw = handle.read(workspace.max_read_bytes + 1)
        truncated = len(raw) > workspace.max_read_bytes
        text = raw[: workspace.max_read_bytes].decode("utf-8", errors="replace")
        return {
            "path": args["path"],
            "content": text,
            "bytes": size,
            "truncated": truncated,
        }

    def write_file(args: Dict[str, Any]) -> Dict[str, Any]:
        path = workspace.resolve(args["path"])
        content = args["content"]
        encoded = content.encode("utf-8")
        if len(encoded) > workspace.max_write_bytes:
            raise ValueError("content exceeds the write limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        return {"path": args["path"], "bytes_written": len(encoded)}

    def list_dir(args: Dict[str, Any]) -> Dict[str, Any]:
        path = workspace.resolve(args.get("path", "."))
        if not path.is_dir():
            raise NotADirectoryError(f"not a directory: {args.get('path', '.')}")
        entries = []
        for child in sorted(path.iterdir())[:100]:
            try:
                size = child.stat().st_size if child.is_file() else None
            except OSError:
                size = None
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": size,
                }
            )
        return {"path": args.get("path", "."), "entries": entries}

    return [
        Tool(
            name="calculator",
            description=(
                "Evaluate a numeric arithmetic expression. Use for exact math. "
                "Supports + - * / // % ** and parentheses. Do not pass prose."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    }
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
            handler=calculator,
        ),
        Tool(
            name="read_file",
            description=(
                "Read a UTF-8 text file inside the workspace. Returns content, "
                "size and a truncated flag. Use list_dir first if unsure of names."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 512}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=read_file,
        ),
        Tool(
            name="write_file",
            description=(
                "Create or overwrite a UTF-8 text file inside the workspace. "
                "Use only when the task explicitly asks to persist content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 512},
                    "content": {"type": "string", "maxLength": 100_000},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=write_file,
            side_effect="write",
        ),
        Tool(
            name="list_dir",
            description=(
                "List files and directories inside the workspace. "
                "Use this before reading files when the layout is unknown."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "maxLength": 512}
                },
                "additionalProperties": False,
            },
            handler=list_dir,
        ),
    ]


def build_default_tools(workspace: str = ".", allow_write: bool = True) -> "ToolRegistry":
    # Keep write tools registered even when writes are disabled: the registry
    # can then return a clear permission_denied error instead of "unknown tool".
    tools = make_file_tools(Workspace(Path(workspace)))
    return ToolRegistry(tools, allow_write=allow_write)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class ToolRegistry:
    tools: List[Tool]
    allow_write: bool = True
    _by_name: Dict[str, Tool] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._by_name = {tool.name: tool for tool in self.tools}
        if len(self._by_name) != len(self.tools):
            raise ValueError("duplicate tool names")

    def specs(self) -> List[Dict[str, Any]]:
        return [tool.spec() for tool in self.tools]

    def names(self) -> List[str]:
        return [tool.name for tool in self.tools]

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._by_name.get(call.name)
        if tool is None:
            return ToolResult.failure(
                "unknown_tool",
                f"unknown tool: {call.name}",
                hint="可用工具: " + ", ".join(self.names()),
            )

        try:
            validate_args(call.arguments, tool.parameters)
        except ValidationError as exc:
            return ToolResult.failure(
                "invalid_argument",
                str(exc),
                retryable=True,
                hint="请按 schema 修正参数后重试",
                field=exc.field,
            )

        if tool.side_effect != "read" and not self.allow_write:
            return ToolResult.failure(
                "permission_denied",
                f"tool {tool.name} requires write access",
                retryable=False,
            )

        # A thread is used only to enforce a wall-clock timeout for this
        # teaching runtime.  It is not a security sandbox: a blocked thread
        # cannot be forcibly killed in Python.  Production code should run
        # untrusted work in a separate process/container.
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(tool.handler, call.arguments)
        try:
            data = future.result(timeout=tool.timeout_s)
        except FutureTimeout:
            future.cancel()
            return ToolResult.failure(
                "timeout",
                f"tool {tool.name} timed out after {tool.timeout_s}s",
                retryable=True,
                hint="缩小任务范围或换一种方法",
            )
        except ValidationError as exc:
            return ToolResult.failure(
                "invalid_argument",
                str(exc),
                retryable=True,
                hint="检查路径是否是工作区内的相对路径",
                field=exc.field,
            )
        except FileNotFoundError as exc:
            return ToolResult.failure(
                "not_found", str(exc), retryable=False, hint="先检查路径"
            )
        except PermissionError as exc:
            return ToolResult.failure(
                "permission_denied", str(exc), retryable=False
            )
        except ValueError as exc:
            return ToolResult.failure(
                "invalid_argument", str(exc), retryable=True
            )
        except Exception as exc:  # noqa: BLE001 - normalize everything for the model
            return ToolResult.failure(
                "internal", f"{type(exc).__name__}: {exc}", retryable=True
            )
        finally:
            # Do not block the loop on a timed-out handler.  A production
            # runtime would isolate it in a process/container and kill it.
            pool.shutdown(wait=False)

        return ToolResult.success(data)

    def describe(self) -> str:
        lines = []
        for tool in self.tools:
            lines.append(f"- {tool.name}: {tool.description}")
        return "\n".join(lines)
