from app import auth


def test_hash_and_verify_roundtrip():
    encoded = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", encoded)


def test_wrong_password_fails():
    encoded = auth.hash_password("correct horse battery staple")
    assert not auth.verify_password("wrong password", encoded)


def test_hashes_are_salted_differently():
    a = auth.hash_password("same password")
    b = auth.hash_password("same password")
    assert a != b  # different random salts
    assert auth.verify_password("same password", a)
    assert auth.verify_password("same password", b)


def test_verify_password_rejects_malformed_hash_without_crashing():
    assert not auth.verify_password("anything", "not-a-valid-hash-format")
    assert not auth.verify_password("anything", "")


def test_session_expiry_is_in_the_future():
    from app.timeutil import utcnow
    assert auth.session_expiry() > utcnow()
