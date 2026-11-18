"""What Django's error reporter may show of a request (divergence 040, slice 6).

Django's `SafeExceptionReporterFilter` masks the request's META entries and settings whose names
match `API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE`, so `HTTP_AUTHORIZATION` (the bearer)
is masked, and `HTTP_MESHYCHAT_CAPABILITY` (an object-capability token, a bearer credential's signed
text) was shown as sent (slice 5's review, I4, measured on Django 6.1.1). Nothing renders a report
today: ADMINS is empty, the one log handler prints time, level, logger and message, and DEBUG is
never on in a deploy. Since slice 6 the header reaches every envelope-path view, so this filter,
set as DEFAULT_EXCEPTION_REPORTER_FILTER, adds CAPABILITY to the names it hides: the header, and
the MESHY_CAPABILITY_* settings with it. Everything else is Django's rule, unchanged.
"""
from __future__ import annotations

import re

from django.views.debug import SafeExceptionReporterFilter


class CapabilityReporterFilter(SafeExceptionReporterFilter):
    hidden_settings = re.compile(SafeExceptionReporterFilter.hidden_settings.pattern
                                 + "|CAPABILITY", flags=re.I)


__all__ = ["CapabilityReporterFilter"]
