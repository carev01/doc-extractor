"""Extraction profiles package.

Importing this package registers all built-in profiles (each profile module
calls registry.register at import). New profile modules are added to the
imports below as they are implemented.
"""

from app.services.profiles import registry  # noqa: F401
from app.services.profiles.base import ExtractionProfile, TocEntry  # noqa: F401

# Import profile modules so they self-register. Order = detection priority.
from app.services.profiles import commvault  # noqa: F401,E402
from app.services.profiles import cohesity  # noqa: F401,E402
from app.services.profiles import docusaurus  # noqa: F401,E402
from app.services.profiles import mkdocs  # noqa: F401,E402
from app.services.profiles import gitbook  # noqa: F401,E402
from app.services.profiles import flare_webhelp  # noqa: F401,E402
from app.services.profiles import flare_html5  # noqa: F401,E402
from app.services.profiles import intercom  # noqa: F401,E402
from app.services.profiles import freshdesk  # noqa: F401,E402
from app.services.profiles import confluence  # noqa: F401,E402
from app.services.profiles import salesforce  # noqa: F401,E402
from app.services.profiles import generic  # noqa: F401,E402
from app.services.profiles import llm  # noqa: F401,E402

__all__ = ["registry", "ExtractionProfile", "TocEntry"]
