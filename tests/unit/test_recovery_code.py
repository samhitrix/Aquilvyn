"""Recovery codes: easy to read back from paper, forgiving to type, hard to guess."""
import re

from identity_svc.api_auth import CODE_ALPHABET, new_recovery_code, normalise_code


def test_codes_are_four_groups_of_four_from_an_unambiguous_alphabet():
    codes = {new_recovery_code() for _ in range(200)}
    assert len(codes) == 200
    for c in codes:
        assert re.fullmatch(r"[A-Z2-9]{4}(-[A-Z2-9]{4}){3}", c)
        assert not set(c.replace("-", "")) & set("01IO") and set(c.replace("-", "")) <= set(CODE_ALPHABET)


def test_typing_case_spaces_and_dashes_do_not_matter():
    assert normalise_code("k7qh 9zpm-4xwd t2nb") == normalise_code("K7QH-9ZPM-4XWD-T2NB") == "K7QH9ZPM4XWDT2NB"
