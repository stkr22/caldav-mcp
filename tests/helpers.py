"""Small helpers for reading tool results with honest types."""

from typing import Any

from fastmcp.tools import ToolResult
from mcp.types import TextContent


def text(result: ToolResult, index: int = 0) -> str:
    """Return the text of one content block, failing if it is not text."""
    block = result.content[index]
    assert isinstance(block, TextContent), type(block)
    return block.text


def structured(result: ToolResult) -> dict[str, Any]:
    """Return the structured content, failing if there is none."""
    assert result.structured_content is not None
    return result.structured_content
