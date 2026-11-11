"""Sifreleme ve parola testleri."""
import unittest

from bot import secretbox


class TestSecretBox(unittest.TestCase):
    def test_roundtrip(self):
        secret = "cok-gizli-master-parola"
        for text in ["", "abc", "APIKEY-1234567890", "unicode: ğüşiöç 🔐", "x" * 5000]:
            token = secretbox.encrypt(text, secret)
            self.assertTrue(token.startswith("v1."))
            self.assertEqual(secretbox.decrypt(token, secret), text)

    def test_wrong_password(self):
        token = secretbox.encrypt("gizli", "parola1")
        with self.assertRaises(ValueError):
            secretbox.decrypt(token, "parola2")

    def test_tamper_detection(self):
        token = secretbox.encrypt("gizli", "parola")
        bad = token[:-6] + ("AAAAAA" if not token.endswith("AAAAAA") else "BBBBBB")
        with self.assertRaises(ValueError):
            secretbox.decrypt(bad, "parola")

    def test_unique_nonce(self):
        a = secretbox.encrypt("ayni metin", "p")
        b = secretbox.encrypt("ayni metin", "p")
        self.assertNotEqual(a, b)

    def test_password_hash(self):
        h, s = secretbox.hash_password("admin1234")
        self.assertTrue(secretbox.verify_password("admin1234", h, s))
        self.assertFalse(secretbox.verify_password("yanlis", h, s))
        h2, s2 = secretbox.hash_password("admin1234")
        self.assertNotEqual(h, h2)  # farkli salt


if __name__ == "__main__":
    unittest.main()
