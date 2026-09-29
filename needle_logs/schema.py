"""Versioned source and record constants for log evidence."""
RECORD_SCHEMA = "kilix.logs.record/v1"
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
ROLES = frozenset({"user", "assistant", "tool", "system", "unknown"})
CHANNELS = frozenset({"message", "tool_request", "tool_result", "lifecycle"})
