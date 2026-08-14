"""Vendor implementations. Import via ``llm.get_provider()``, not directly.

Each module is imported lazily by ``llm/factory.py`` so a missing optional SDK
only breaks the provider that needs it — running on Ollama never requires the
``openai`` or ``anthropic`` packages to be installed.
"""
