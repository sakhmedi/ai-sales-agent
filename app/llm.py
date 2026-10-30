"""Constants shared by every Claude call in the app."""

# On a safety decline, the API retries the request on a fallback model by itself.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Returns the model's short progress notes between tool calls, so we can log them.
THINKING_UPDATES_BETA = "thinking-display-updates-2026-08-18"
