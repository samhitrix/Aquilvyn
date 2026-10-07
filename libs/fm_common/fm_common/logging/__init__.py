"""Context-Aware Logging Engine.

* structlog + contextvars: trace_id / user_id / household_id / client_ip are bound once per
  request by middleware and automatically merged into every log line.
* PII masking processor redacts sensitive keys and scrubs PAN / Aadhaar / card / email /
  phone patterns from free-text values before rendering.
* Single-line JSON to stdout; the actual write happens on a background thread
  (QueueHandler -> QueueListener) so the event loop never blocks on I/O.
"""
from .setup import bind_context, clear_context, configure_logging, get_logger

__all__ = ["bind_context", "clear_context", "configure_logging", "get_logger"]
