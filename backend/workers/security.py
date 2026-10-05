"""Security & Health Agent worker — detects, records and (SA4) contains; never trades."""
from workers.base import main

if __name__ == "__main__":
    from security_agent.redact import install_log_filter
    from security_agent.runner import loop
    install_log_filter()
    main("security", [loop])
