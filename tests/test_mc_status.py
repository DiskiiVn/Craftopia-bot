import unittest

from mc_status import _encode_varint, _read_exact, _read_varint


class FakeSocket:
    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)

    def recv(self, length: int) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) <= length:
            return chunk
        self.chunks.insert(0, chunk[length:])
        return chunk[:length]


class VarIntTests(unittest.TestCase):
    def test_encode_varint_matches_minecraft_vectors(self):
        vectors = (
            (0, b"\x00"),
            (1, b"\x01"),
            (127, b"\x7f"),
            (128, b"\x80\x01"),
            (255, b"\xff\x01"),
            (2_147_483_647, b"\xff\xff\xff\xff\x07"),
            (-1, b"\xff\xff\xff\xff\x0f"),
            (-2_147_483_648, b"\x80\x80\x80\x80\x08"),
        )

        for value, encoded in vectors:
            with self.subTest(value=value):
                self.assertEqual(_encode_varint(value), encoded)

    def test_read_varint_round_trips_positive_values(self):
        for value in (0, 1, 127, 128, 255, 2_147_483_647):
            with self.subTest(value=value):
                encoded = _encode_varint(value)
                connection = FakeSocket([bytes([byte]) for byte in encoded])
                self.assertEqual(_read_varint(connection), value)

    def test_read_varint_rejects_more_than_five_bytes(self):
        connection = FakeSocket([b"\x80"] * 6)

        with self.assertRaisesRegex(ValueError, "VarInt quá dài"):
            _read_varint(connection)

    def test_read_varint_rejects_early_disconnect(self):
        connection = FakeSocket([b"\x80", b""])

        with self.assertRaisesRegex(ConnectionError, "đóng kết nối sớm"):
            _read_varint(connection)

    def test_read_exact_accepts_fragmented_socket_reads(self):
        connection = FakeSocket([b"ab", b"c", b"def"])

        self.assertEqual(_read_exact(connection, 6), b"abcdef")

    def test_read_exact_rejects_early_disconnect(self):
        connection = FakeSocket([b"ab", b""])

        with self.assertRaisesRegex(ConnectionError, "đóng kết nối sớm"):
            _read_exact(connection, 3)


if __name__ == "__main__":
    unittest.main()
