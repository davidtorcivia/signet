"""Capabilities.

Drop a module in here that exports `CAPABILITIES: list[Capability]` and it is live: callable
by the agent, routable, permission-checked, logged, and exposed over MCP if its `exposure`
says so. Nothing outside the module needs editing.
"""
