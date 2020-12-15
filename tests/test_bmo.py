# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import mock
import json
import pytest

from mozphab.bmo import bmo, BMOAPIError


@mock.patch("mozphab.bmo.BMOAPI.get")
@mock.patch("mozphab.bmo.conduit")
def test_whoami(m_conduit, m_get):
    bmo.whoami()
    m_get.assert_called_once_with("whoami", headers={"X-PHABRICATOR-TOKEN": mock.ANY})
    m_conduit.load_api_token.assert_called_once()


@mock.patch("mozphab.bmo.conduit")
def test_build_request(m_conduit):
    m_conduit.repo.bmo_url = "https://bmo.test"

    assert bmo._build_request(method="test_method") == {
        "url": "https://bmo.test/rest/test_method",
        "method": "GET",
        "headers": {"User-Agent": mock.ANY},
    }

    assert bmo._build_request(method="test_method", headers={"X-Test": "true"}) == {
        "url": "https://bmo.test/rest/test_method",
        "method": "GET",
        "headers": {"User-Agent": mock.ANY, "X-Test": "true"},
    }


def test_sanitised_req():
    assert bmo._sanitise_req(
        {
            "url": "https://bmo.test/rest/test_method",
            "method": "GET",
            "headers": {"X-PHABRICATOR-TOKEN": "cli-secret"},
        }
    ) == {
        "url": "https://bmo.test/rest/test_method",
        "method": "GET",
        "headers": {"X-PHABRICATOR-TOKEN": "cli-XXXX"},
    }


@mock.patch("urllib.request.urlopen")
@mock.patch("mozphab.bmo.conduit")
def test_get(m_conduit, m_urlopen):
    m_conduit.repo.bmo_url = "https://bmo.test"

    # build fake context-manager to mock urlopen
    cm = mock.MagicMock()
    cm.getcode.return_value = 200
    cm.__enter__.return_value = cm
    m_urlopen.return_value = cm

    # success
    cm.read.return_value = json.dumps({"result": "result"})
    assert bmo.get("method") == {"result": "result"}

    # error
    cm.read.return_value = json.dumps({"error": "aieee"})
    with pytest.raises(BMOAPIError):
        bmo.get("method")
