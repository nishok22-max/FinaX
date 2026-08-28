"""Optional agentic layer over the deterministic keeper (FR-18…FR-22).

Deliberately empty of imports. ``app/agent`` depends on an optional install extra
(``pip install -e ".[agent]"``); anything imported here would be imported by every module that
touches the package, which is exactly what :mod:`tests.test_agent_isolation` forbids. Import
submodules directly, and route third-party agent imports through :mod:`app.agent._lazy`.
"""
