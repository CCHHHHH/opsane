"""Safe, read-only ingestion of files attached to chat sessions."""

from shell_agent.attachments.parser import (
    OFFICE_PREVIEW_EXTENSIONS,
    ParsedAttachment,
    RenderedOfficePreview,
    parse_attachment,
    render_office_pdf,
)

__all__ = [
    "OFFICE_PREVIEW_EXTENSIONS",
    "ParsedAttachment",
    "RenderedOfficePreview",
    "parse_attachment",
    "render_office_pdf",
]
