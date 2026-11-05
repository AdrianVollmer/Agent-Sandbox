from datetime import datetime

from agent_sandbox.accesslog import Denial, denied_destinations

LOG = """\
1793872474.321      0 10.89.0.3 TCP_DENIED/403 3385 CONNECT pypi.org:443 - HIER_NONE/- text/html
1793872475.000     12 10.89.0.3 TCP_TUNNEL/200 95 CONNECT api.anthropic.com:443 - HIER_DIRECT/1.2.3.4 -
1793872476.326      0 10.89.0.3 TCP_DENIED/403 3387 GET http://Example.com/path?q=1 - HIER_NONE/- text/html
1793872477.500      0 10.89.0.3 TCP_DENIED/403 3385 CONNECT pypi.org:443 - HIER_NONE/- text/html
1793872478.000      0 10.89.0.3 TCP_DENIED/403 3383 CONNECT [fd00::1]:8443 - HIER_NONE/- text/html
garbage line
1793872479.000      0 10.89.0.3 NONE_NONE/400 0 GET error:invalid-request - HIER_NONE/- -
"""


def test_denied_destinations_groups_and_sorts_newest_first() -> None:
    assert denied_destinations(LOG.splitlines()) == [
        Denial(host="fd00::1", port=8443, count=1, last_seen=datetime.fromtimestamp(1793872478.0).astimezone()),
        Denial(host="pypi.org", port=443, count=2, last_seen=datetime.fromtimestamp(1793872477.5).astimezone()),
        Denial(host="example.com", port=80, count=1, last_seen=datetime.fromtimestamp(1793872476.326).astimezone()),
    ]


def test_denied_destinations_empty() -> None:
    assert denied_destinations([]) == []
