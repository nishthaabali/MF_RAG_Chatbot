"""PII guard: >= 5 positive and >= 5 negative cases per kind (docs/edge-case.md §4)."""

import pytest

from app.pipeline.pii_guard import WITHHELD_PLACEHOLDER, check_pii

POSITIVE = {
    "pan": [
        "My PAN is ABCDE1234F",
        "pan: abcde1234f",  # PII-02 lower case
        "PAN AbCdE1234f please check",
        "ABCDE 1234 F is my pan",  # PII-03 separators
        "ABCDE-1234-F",
        "here it is: ABCDE1234F",  # PII-16 follow-up
    ],
    "aadhaar": [
        "my aadhaar 1234 5678 9012",  # PII-04
        "1234-5678-9012",
        "123456789012",
        "Aadhaar no. 2345 6789 0123 linked?",
        "uid 9876-5432-1098",
    ],
    "phone": [
        "call me on 9876543210",  # PII-05
        "+91 98765 43210",
        "+91-9876543210",
        "09876543210",
        "my number is 98765-43210",
        "reach me at 987 654 3210",
    ],
    "email": [
        "me@gmail.com",  # PII-06
        "mail First.Last+mf@domain.co.in the details",
        "me at gmail dot com",  # PII-07 spelt out
        "me[at]gmail[dot]com",
        "contact: rahul (at) yahoo (dot) in",
    ],
    "upi": [
        "pay to name@okhdfcbank",  # PII-14
        "my upi is 98765@ybl",
        "rahul.k@paytm",
        "send to abc@oksbi",
        "vpa test-1@upi",
    ],
    "otp": [
        "my OTP is 482913",  # PII-08
        "code 4821",
        "the pin is 1234",
        "482913 is the otp I got",
        "One-time password: 556677",
        "mpin 9090",
    ],
    "account": [
        "folio no 12345678/90",  # PII-09
        "a/c 001234567890",
        "account number 50100123456789",
        "my folio is 1234567",
        "bank acct 3344556677",
        "demat account 1203450000123456",
    ],
    "masked_id": [
        "PAN ending XXXX1234F",  # PII-28: masked identifiers are still blocked
        "a/c ****5678",
        "card number xxxx-xxxx-4321",
        "account no **3456",
        "folio XX9876",
    ],
    "long_number": [
        "4111111111111111",  # PII-15 card number without a keyword
        "reference 123456789",
        "send 9123456789012345 there",
        "id 5555444433",
        "number 100200300400",
    ],
}

NEGATIVE = {
    "pan": [
        "Is PAN mandatory for investing?",  # PII-23
        "ISIN INF209K01YN0",  # PII-25
        "What is the NAV of ABSL Small Cap?",
        "returns since 2013 a fund made",
        "ICICI Pru Bluechip expense ratio",
    ],
    "aadhaar": [
        "Do I need Aadhaar for KYC?",  # PII-23
        "₹10,00,000 lumpsum",  # PII-26
        "launched on 01/01/2013",  # PII-22
        "AUM is ₹2,620.50 Cr as of 06 Nov 2026",
        "NIFTY 100 and BSE 500",  # PII-21
    ],
    "phone": [
        "Minimum SIP is ₹1000?",  # PII-20
        "Can I start a ₹500 SIP?",
        "AMFI helpline 1800-425-3800",
        "NAV ₹16.97 on 06-11-2026",
        "Nifty Next 50 index, 3 year return 17.06%",
    ],
    "email": [
        "How do I update my email in my folio?",
        "Is email needed for KYC?",
        "What is the exit load at present for Kotak?",
        "the fund house is located at Mumbai",
        "send the statement by email please",
    ],
    "otp": [
        "What is an OTP?",  # PII-23
        "AMFI scheme code 120503",  # PII-25
        "Is OTP needed to redeem units?",
        "code of conduct for distributors",
        "pin code of the AMC office",
    ],
    "account": [
        "How do I find my folio number?",  # PII-24
        "How do I download my account statement?",
        "minimum SIP for my account is 1000",
        "₹10,00,000 lumpsum in my bank account",
        "Is a bank account needed for SIP?",
    ],
}

ALL_NEGATIVE = [msg for msgs in NEGATIVE.values() for msg in msgs]


@pytest.mark.parametrize(
    ("kind", "message"), [(k, m) for k, msgs in POSITIVE.items() for m in msgs]
)
def test_blocks_personal_data(kind, message):
    result = check_pii(message)
    assert result.blocked
    assert kind in result.kinds


@pytest.mark.parametrize("message", ALL_NEGATIVE)
def test_does_not_block_ordinary_questions(message):
    result = check_pii(message)
    assert not result.blocked, result.kinds


def test_enough_cases_per_kind():
    for kind in ("pan", "aadhaar", "phone", "email", "otp", "account"):
        assert len(POSITIVE[kind]) >= 5 and len(NEGATIVE[kind]) >= 5, kind


@pytest.mark.parametrize(
    "message",
    [
        "9876​543210",  # PII-12 zero-width space inside the number
        "9876 543210",  # non-breaking space
        "९८७६५४५३२१",  # PII-13 Devanagari
        "９８７６５４３２１０",  # full-width digits
    ],
)
def test_unicode_tricks_are_normalised(message):
    assert "phone" in check_pii(message).kinds


def test_pii_buried_in_a_long_question_blocks_the_whole_message():  # PII-10
    msg = (
        "What is the exit load of Kotak Multi Cap Fund and the minimum SIP, also my PAN "
        "is ABCDE1234F so can you check the expense ratio too?"
    )
    assert check_pii(msg).blocked


def test_several_kinds_are_all_listed():  # PII-11
    result = check_pii("PAN ABCDE1234F, phone 9876543210, mail me@x.com, OTP 4821")
    assert {"pan", "phone", "email", "otp"} <= set(result.kinds)


def test_result_never_contains_the_matched_text():  # PII-31
    result = check_pii("My PAN is ABCDE1234F and phone 9876543210")
    assert "ABCDE1234F" not in repr(result)
    assert "9876543210" not in repr(result)
    assert set(vars(result)) == {"blocked", "kinds"}


def test_placeholder_text():
    assert WITHHELD_PLACEHOLDER == "[message withheld: contained personal information]"
