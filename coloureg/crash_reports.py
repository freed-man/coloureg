"""What a crash report must never carry: a secret (paint292).

Sentry is sent the failing call's local variables and the outbound calls made
before it. Measured on 8 Oct 2026 with the site's own set-up, against local
stand-ins, nothing real involved:

  * a lookup whose VDG reply could not be read sent the VDG key twice: in the
    `params` variable, and in the outbound call's query string, which the
    library keeps as a breadcrumb;
  * a failed database connection sent the database password four times: in
    `conn_params`, `args`, `kwargs`, and inside the `dsn` string.

The library's own filter looks at the top-level NAME of a variable and nothing
else, so a secret inside a dict, a list or a longer string walks straight past
it. Three nets here, because each alone has holes:

  1. BY VALUE. Every secret this process was given (an environment variable
     whose name says it is one, and the password inside one that is an
     address) is replaced wherever it appears: a variable, a dict, a query
     string, a message. This is the net that cannot be dodged by a new
     variable name.
  2. BY PARAMETER. `apikey=...`, `password=...` and the like are replaced
     whatever their value, so a secret that did not come from the environment
     is caught as well.
  3. BY NAME, AT ANY DEPTH. settings.py switches the library's filter to look
     inside dicts and lists too (EventScrubber(recursive=True)).

Nothing here touches a customer's details. Whether a report may carry those is
a separate decision, and a separate release.

No Django import: settings.py loads this before the apps exist.
"""
import logging
import os
import re
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

LABEL = '[secret]'

#: An environment variable with one of these in its name holds a secret.
_SECRET_NAME = re.compile(r'KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|DSN|DATABASE_URL', re.I)

#: Shorter than this is not replaced by value: swapping every "abc" in a report
#: for a label would wreck the report and protect nothing.
_MIN_LENGTH = 8

_NAMES = (r'apikey|api_key|api-key|access_token|client_secret|csrfmiddlewaretoken|'
          r'key|token|secret|password|passwd|pwd|signature|sig')
#: name=value, as in a query string or a database address.
_PARAMETER = re.compile(r'(?i)(?<![A-Za-z0-9_])(' + _NAMES + r')=([^&\s\'"<>]+)')
#: 'name': 'value', as in a dict the library has already turned into text.
_QUOTED = re.compile(r'(?i)([\'"])(' + _NAMES + r'|authorization)\1(\s*:\s*)([\'"])(.*?)\4')


def secrets():
    """Every secret this process was given, longest first.

    Read from the environment on each call, not kept: a key that is rotated is
    covered from the next report on."""
    found = set()
    for name, value in os.environ.items():
        if not _SECRET_NAME.search(name) or not value:
            continue
        found.add(value)
        if '://' in value:
            # An address with a password in it (DATABASE_URL). The password
            # travels on its own once the address has been taken apart.
            try:
                password = urlsplit(value).password
            except ValueError:
                password = None
            if password:
                found.add(password)
                found.add(unquote(password))
    return sorted((s for s in found if len(s) >= _MIN_LENGTH), key=len, reverse=True)


#: The lines of the site's own code that the library sends round a failing
#: line. They hold no secret (the code is public), and net 2 would turn
#: `sorted(rows, key=len)` into `sorted(rows, key=[secret]`. Net 1 still runs.
_SOURCE_LINES = frozenset({'pre_context', 'context_line', 'post_context'})


def clean_text(text, known=None, by_parameter=True):
    """One string with every secret replaced by a label."""
    for secret in (secrets() if known is None else known):
        if secret in text:
            text = text.replace(secret, LABEL)
    if not by_parameter:
        return text
    text = _PARAMETER.sub(lambda m: f'{m.group(1)}={LABEL}', text)
    return _QUOTED.sub(lambda m: f'{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{m.group(4)}{LABEL}{m.group(4)}', text)


def _clean(node, known, by_parameter=True):
    if isinstance(node, str):
        return clean_text(node, known, by_parameter)
    if isinstance(node, dict):
        return {key: _clean(value, known, by_parameter and key not in _SOURCE_LINES)
                for key, value in node.items()}
    if isinstance(node, list):
        return [_clean(value, known, by_parameter) for value in node]
    if isinstance(node, tuple):
        return tuple(_clean(value, known, by_parameter) for value in node)
    return node


def strip_secrets(event):
    """The whole event, every string at any depth, with secrets replaced.

    Returns None when it cannot do that: a report that cannot be cleaned is not
    sent, and the log says so."""
    try:
        return _clean(event, secrets())
    except Exception:           # noqa: BLE001 - never a report with a secret in it
        logger.warning('a crash report could not be cleaned of secrets and was not sent')
        return None
