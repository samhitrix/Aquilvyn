"""Service-to-service hops carry the user's token (permissions) but never spend the user's request budget:
one analysis run fanned out into hundreds of internal calls and locked the person out of every page with 429."""
from fm_common.http import _headers
from fm_common.identity.tokens import INTERNAL_HEADER, internal_proof
from fm_common.ratelimit.middleware import _is_internal


def scope(headers):
    return {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}


def test_an_internal_hop_is_recognised_and_a_browser_cannot_fake_it():
    h = _headers("user-token-abc")
    assert h[INTERNAL_HEADER] == internal_proof("user-token-abc") and _is_internal(scope(h))
    assert not _is_internal(scope({"Authorization": "Bearer user-token-abc"}))                                   # browser
    assert not _is_internal(scope({"Authorization": "Bearer user-token-abc", INTERNAL_HEADER: "0" * 40}))        # forged
    assert not _is_internal(scope({"Authorization": "Bearer other-token", INTERNAL_HEADER: h[INTERNAL_HEADER]}))  # replayed
